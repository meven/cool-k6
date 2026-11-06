# Steps shared by the scripts in deploy/ that measure outside the timer, sourced from the root of
# the checkout after /etc/cool-perf.env.

# Take the lock of deploy/nightly on file descriptor 9, so that no timer run starts during this
# one. With $1 set, wait for the run that holds the lock to end. A timer run that starts
# meanwhile waits in turn, and runs once this one has ended.
take_lock() {
    local state=${XDG_STATE_HOME:-$HOME/.local/state}/cool-perf
    exec 9>"$state/lock"
    if [ -n "${1:-}" ]; then
        flock -n 9 || { echo "=== waiting for the run of the timer to end"; flock 9; }
    else
        flock -n 9 || { echo "a nightly run is running; stop cool-perf.timer, wait, or give --wait" >&2
                        exit 1; }
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
