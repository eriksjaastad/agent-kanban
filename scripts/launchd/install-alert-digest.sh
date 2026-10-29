#!/usr/bin/env bash
#
# Install / uninstall the portfolio alert-digest LaunchAgent on this machine.
#
# The .plist.in template holds @PT_HOME@ placeholders instead of a hardcoded
# username. This script substitutes @PT_HOME@ from the local checkout, writes a
# rendered .plist to ~/Library/LaunchAgents/, and loads it. That keeps the repo
# machine-agnostic — launchd cannot expand $HOME, so the substitution happens
# here at install time.
#
# Usage:
#   scripts/launchd/install-alert-digest.sh install
#   scripts/launchd/install-alert-digest.sh uninstall
#   scripts/launchd/install-alert-digest.sh status
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PT_HOME="$(cd "$SCRIPT_DIR/../.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL="local.project-tracker.alert-digest"
SRC="$SCRIPT_DIR/${LABEL}.plist.in"
DST="$LAUNCH_AGENTS_DIR/${LABEL}.plist"

install_agent() {
    if [[ ! -f "$SRC" ]]; then
        echo "install: template missing: $SRC" >&2
        exit 2
    fi
    mkdir -p "$LAUNCH_AGENTS_DIR" "$PT_HOME/logs"

    # | delimiter so slashes in paths don't need escaping.
    sed -e "s|@PT_HOME@|$PT_HOME|g" "$SRC" > "$DST"

    # Fail loudly if any placeholder leaked through.
    if grep -qE '@[A-Z_]+@' "$DST"; then
        echo "install: unsubstituted placeholder in $DST:" >&2
        grep -nE '@[A-Z_]+@' "$DST" >&2
        exit 2
    fi

    launchctl unload "$DST" 2>/dev/null || true
    launchctl load -w "$DST"
    echo "loaded: $DST (fires 7:00 AM local)"
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
