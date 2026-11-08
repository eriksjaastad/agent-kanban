#!/usr/bin/env bash
#
# Install / uninstall the website stats LaunchAgent (#8868), which runs
# `pt site-stats run` daily at 6:30 AM local. The owner runs this: agents cannot
# load launchd jobs on this Mac.
#
# Usage:
#   scripts/launchd/install-site-stats.sh install
#   scripts/launchd/install-site-stats.sh uninstall
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PT_HOME="$(cd "$SCRIPT_DIR/../.." && pwd)"
LABEL="local.project-tracker.site-stats"
SRC="$SCRIPT_DIR/${LABEL}.plist.in"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
    install)
        mkdir -p "$(dirname "$DST")" "$PT_HOME/logs"
        tmp="$DST.tmp"
        sed -e "s|@PT_HOME@|$PT_HOME|g" -e "s|@HOME@|$HOME|g" "$SRC" > "$tmp"
        if grep -nE '@[A-Z_]+@' "$tmp" >&2; then
            echo "install: unsubstituted placeholder above in $tmp" >&2
            exit 2
        fi
        mv "$tmp" "$DST"
        launchctl bootout "$DOMAIN" "$DST" 2>/dev/null || true  # not loaded yet is fine
        launchctl bootstrap "$DOMAIN" "$DST"
        echo "loaded: $DST (daily 6:30 AM local; run now: launchctl kickstart $DOMAIN/$LABEL)"
        ;;
    uninstall)
        if [[ ! -f "$DST" ]]; then
            echo "skipped (not installed): $DST"
            exit 0
        fi
        launchctl bootout "$DOMAIN" "$DST" 2>/dev/null || true
        trash "$DST"
        echo "removed: $DST"
        ;;
    *)
        echo "usage: $0 {install|uninstall}" >&2
        exit 2
        ;;
esac
