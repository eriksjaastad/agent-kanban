#!/usr/bin/env bash
# Lint dashboard/static/ vanilla JS files for global scope collisions.
# Uses the frontend's eslint installation via symlink.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# Temporarily symlink node_modules so eslint resolves @eslint/js
ln -sf dashboard/frontend/node_modules node_modules
trap 'trash node_modules 2>/dev/null || true' EXIT

dashboard/frontend/node_modules/.bin/eslint -c eslint.static.config.mjs dashboard/static/
