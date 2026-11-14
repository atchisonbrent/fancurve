#!/bin/sh
# Build and install locally, inspect the job, or return the fan to macOS and
# uninstall. Run from a checkout with ./install.sh [install|status|uninstall].
set -eu
cd "$(dirname "$0")"

LABEL=io.github.atchisonbrent.fancurve
# These overrides exist for the tests.
dest=${FANCURVE_DEST:-/usr/local/libexec/fancurve}
jobs=${FANCURVE_JOBS:-/Library/LaunchDaemons}
logs=${FANCURVE_LOGS:-/var/log}
label=$LABEL

begin_privileged() {
    use_sudo=0
    if [ "$(id -u)" != 0 ]; then
        use_sudo=1
        # Ask for the password once, up front. Where sudo needs none, `sudo -v`
        # can still demand one, so it is skipped.
        sudo -n true 2>/dev/null || sudo -v
    fi
}
as_root() {
    if [ "$use_sudo" = 1 ]; then sudo "$@"; else "$@"; fi
}
# 0: loaded. 1: launchd says there is no such job (its exit 113). Anything else
# ends the script: a query that failed is not an answer.
job_loaded() {
    rc=0
    as_root launchctl print "system/$label" >/dev/null 2>&1 || rc=$?
    case "$rc" in
    0) return 0 ;;
    113) return 1 ;;
    *) echo "cannot tell whether $label is loaded (launchctl print exit $rc); stopping here" >&2; exit 1 ;;
    esac
}
show_status() {
    rc=0
    job=$(as_root launchctl print "system/$label" 2>/dev/null) || rc=$?
    if [ "$rc" = 113 ]; then echo "$label is not loaded"; rc=1
    elif [ "$rc" != 0 ]; then echo "cannot tell whether $label is loaded (launchctl print exit $rc)"; return 1
    else
        echo "$job" | grep -E '^[[:space:]]*(state|pid|last exit code) = ' | head -3
        echo "$job" | grep -q '^[[:space:]]*state = running' || { echo "$label is loaded but not running"; rc=1; }
    fi
    "$dest/fancurve" status "$dest/fancurve.conf" || rc=1
    [ ! -f "$logs/fancurve.log" ] || tail -n 12 "$logs/fancurve.log"
    return "$rc"
}

case "${1:-install}" in
install)
    [ "$(uname -m)" = arm64 ] || { echo "fancurve needs an Apple Silicon Mac" >&2; exit 1; }
    command -v cc >/dev/null 2>&1 || { echo "install the Xcode Command Line Tools first: xcode-select --install" >&2; exit 1; }
    tmp=
    staged=0
    old_stopped=0
    phase=before
    # Report only what this run established, and remove temporary build and
    # staged files on failure. Installed files stay available for recovery.
    trap 'rm -rf "$tmp"
    if [ "$staged" = 1 ]; then
        as_root rm -rf "$dest.new" || echo "could not remove staged files at $dest.new" >&2
    fi
    case "$phase" in
    stopping) echo "failed while stopping the old controller: it may or may not be running, and the fan was not checked. Run status, then install again." >&2 ;;
    stopped)
        if [ "$old_stopped" = 1 ]; then
            echo "the old controller is stopped, but the fan is NOT confirmed back with macOS; nothing new was installed. Run status, then install again." >&2
        else
            echo "the fan is NOT confirmed with macOS (automatic mode could not be written and read back); nothing was installed." >&2
        fi ;;
    testing)
        if [ "$old_stopped" = 1 ]; then
            echo "write test failed; nothing new was installed; the previous controller is stopped and its files are still in place." >&2
        else
            echo "write test failed; nothing new was installed; any previous files are still in place." >&2
        fi ;;
    released) echo "the controller is NOT running; macOS had the fan when it stopped (written and read back). Fix the error above and install again." >&2 ;;
    starting) echo "the old controller was stopped and macOS then had the fan, but the new one is not confirmed running: it may or may not be. Run status." >&2 ;;
    esac' 0
    # An explicit template: macOS mktemp otherwise ignores TMPDIR.
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/fancurve-build.XXXXXX")
    cc -O2 -Wall -Wextra fancurve.c -o "$tmp/fancurve" -framework IOKit -framework CoreFoundation
    sh -n start.sh
    plutil -lint "$label.plist" >/dev/null
    # Check the settings, fan and sensors without root before touching the job.
    "$tmp/fancurve" check fancurve.conf
    begin_privileged
    # Stage beside the destination so replacement requires only renames.
    staged=1
    as_root rm -rf "$dest.new" "$dest.old"
    as_root install -d -o root -g wheel -m 755 "$dest.new"
    as_root install -o root -g wheel -m 755 "$tmp/fancurve" "$dest.new/fancurve"
    as_root install -o root -g wheel -m 755 start.sh "$dest.new/start"
    as_root install -o root -g wheel -m 644 fancurve.conf "$dest.new/fancurve.conf"
    as_root install -o root -g wheel -m 644 "$label.plist" "$dest.new/$label.plist"
    if job_loaded; then
        phase=stopping
        as_root launchctl bootout "system/$label"
        n=0
        while job_loaded; do
            n=$((n + 1)); [ "$n" -le 20 ] || { echo "launchd still has $label" >&2; exit 1; }
            sleep 0.5
        done
        old_stopped=1
    fi
    # Confirm the new binary can return the fan, even if the old one could not.
    phase=stopped
    as_root "$dest.new/fancurve" auto
    phase=testing
    as_root "$dest.new/fancurve" selftest "$dest.new/fancurve.conf"
    phase=released
    [ ! -e "$dest" ] || as_root mv "$dest" "$dest.old"
    as_root mv "$dest.new" "$dest"
    staged=0
    as_root mv "$dest/$label.plist" "$jobs/$label.plist"
    phase=starting
    as_root launchctl bootstrap system "$jobs/$label.plist"
    sleep 3
    job=$(as_root launchctl print "system/$label") &&
        printf '%s\n' "$job" | grep -q '^[[:space:]]*state = running' || { echo "$label was started but is not confirmed running" >&2; exit 1; }
    phase=done
    as_root rm -rf "$dest.old" || echo "installed and running; the previous copy at $dest.old could not be deleted" >&2
    show_status || { echo "fancurve is installed and its job is running, but status found a problem (above)" >&2; exit 1; }
    echo "fancurve is installed and running; log: $logs/fancurve.log"
    echo "Status: ./install.sh status"
    echo "Uninstall: ./install.sh uninstall"
    ;;
status)
    begin_privileged
    show_status
    ;;
uninstall)
    begin_privileged
    loaded=0
    if job_loaded; then
        loaded=1
        as_root launchctl bootout "system/$label"
        n=0
        while job_loaded; do
            n=$((n + 1)); [ "$n" -le 20 ] || { echo "launchd still has $label" >&2; exit 1; }
            sleep 0.5
        done
    fi
    if [ "$loaded" = 0 ] && [ ! -e "$dest" ] && [ ! -e "$jobs/$label.plist" ]; then
        echo "nothing to remove"
        exit 0
    fi
    # `auto` writes automatic mode and reads it back: nothing is deleted
    # without that confirmation.
    [ -x "$dest/fancurve" ] || { echo "no controller binary to confirm the fan's mode with; nothing was deleted. Install again, then uninstall." >&2; exit 1; }
    as_root "$dest/fancurve" auto || { echo "the job is stopped, but the fan is not confirmed back with macOS; nothing was deleted" >&2; exit 1; }
    as_root rm -f "$jobs/$label.plist" "$logs/fancurve.log" "$logs/fancurve.log.1"
    as_root rm -rf "$dest" "$dest.new" "$dest.old"
    echo "uninstalled; macOS has the fan"
    ;;
*)
    echo "usage: $0 [install|status|uninstall]" >&2
    exit 64
    ;;
esac
