#!/usr/bin/env bash
#
# Install / uninstall the project-tracker dashboard LaunchAgent on this machine.
#
# Substitutes @PT_HOME@ in the .plist.in template from the local checkout and
# loads it. launchd cannot expand $HOME, so substitution happens here.
#
# #6887 committed the dashboard's plist as a template — it was the one job whose
# config lived only in ~/Library/LaunchAgents, untracked. #6909 adds this script
# so installing it matches its siblings instead of a hand-run sed:
#   - a placeholder-leak guard, so a typo'd substitution fails loudly rather
#     than installing a plist containing a literal @PT_HOME@
#   - unload/load rather than `launchctl kickstart -k`, which only restarts an
#     already-bootstrapped job and does nothing on a fresh machine
#
# Usage:
#   scripts/launchd/install-dashboard.sh install
#   scripts/launchd/install-dashboard.sh uninstall
#   scripts/launchd/install-dashboard.sh status
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PT_HOME="$(cd "$SCRIPT_DIR/../.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL="local.project-tracker.dashboard"
SRC="$SCRIPT_DIR/${LABEL}.plist.in"
DST="$LAUNCH_AGENTS_DIR/${LABEL}.plist"

install_agent() {
    if [[ ! -f "$SRC" ]]; then
        echo "install: template missing: $SRC" >&2
        exit 2
    fi
    if [[ ! -x "$PT_HOME/scripts/launch-dashboard.sh" ]]; then
        echo "install: launcher missing or not executable: $PT_HOME/scripts/launch-dashboard.sh" >&2
        echo "         the plist points at it; installing now would give a job that cannot start" >&2
        exit 2
    fi
    mkdir -p "$LAUNCH_AGENTS_DIR" "$PT_HOME/logs"
    sed -e "s|@PT_HOME@|$PT_HOME|g" "$SRC" > "$DST"
    if grep -qE '@[A-Z_]+@' "$DST"; then
        echo "install: unsubstituted placeholder in $DST:" >&2
        grep -nE '@[A-Z_]+@' "$DST" >&2
        exit 2
    fi
    launchctl unload "$DST" 2>/dev/null || true
    launchctl load -w "$DST"
    echo "loaded: $DST (dashboard on http://127.0.0.1:8000)"
    echo "verify: curl -s -o /dev/null -w '%{http_code}\\n' http://localhost:8000/api/health"
}

uninstall_agent() {
    if [[ -f "$DST" ]]; then
        launchctl unload "$DST" 2>/dev/null || true
        trash "$DST" 2>/dev/null || command rm -f "$DST"
        echo "removed: $DST"
    else
        echo "skipped (not installed): $DST"
    fi
}

status_agent() {
    if [[ -f "$DST" ]]; then
        echo "installed: $DST"
        launchctl list | awk -v l="$LABEL" '$3 == l { print "  launchctl: pid="$1" exit="$2" label="$3 }'
    else
        echo "not installed: $DST"
    fi
}

case "${1:-}" in
    install)   install_agent ;;
    uninstall) uninstall_agent ;;
    status)    status_agent ;;
    *)
        echo "usage: $0 {install|uninstall|status}" >&2
        exit 2
        ;;
esac
