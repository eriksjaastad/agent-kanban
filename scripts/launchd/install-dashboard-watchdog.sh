#!/usr/bin/env bash
#
# Install / uninstall the dashboard health watchdog LaunchAgent on this machine.
#
# Substitutes @PT_HOME@ in the .plist.in template from the local checkout and
# loads it. launchd cannot expand $HOME, so substitution happens here.
#
# Usage:
#   scripts/launchd/install-dashboard-watchdog.sh install
#   scripts/launchd/install-dashboard-watchdog.sh uninstall
#   scripts/launchd/install-dashboard-watchdog.sh status
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PT_HOME="$(cd "$SCRIPT_DIR/../.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL="local.project-tracker.dashboard-watchdog"
SRC="$SCRIPT_DIR/${LABEL}.plist.in"
DST="$LAUNCH_AGENTS_DIR/${LABEL}.plist"

install_agent() {
    if [[ ! -f "$SRC" ]]; then
        echo "install: template missing: $SRC" >&2
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
    echo "loaded: $DST (checks every 5 min)"
}

uninstall_agent() {
    if [[ -f "$DST" ]]; then
        launchctl unload "$DST" 2>/dev/null || true
        rm -f "$DST"
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
