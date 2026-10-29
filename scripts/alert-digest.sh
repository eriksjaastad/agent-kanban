#!/usr/bin/env bash
# Run the portfolio alert digest with launchd's minimal PATH fixed, so the
# digest can find doppler: it runs itself under the Doppler project named in
# the local config's [digest] section (see "Local integrations" in
# scripts/config.py). Used by the alert-digest launchd plist (7:00 AM local).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# launchd has a minimal PATH — put brew (doppler) and the venv on it.
BREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
export PATH="$HOME/.local/bin:$BREW_PREFIX/bin:$PATH"

exec "$PROJECT_DIR/.venv/bin/python3" "$SCRIPT_DIR/alert_digest.py" "$@"
