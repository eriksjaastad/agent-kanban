# project-tracker — local convenience targets (#6065).
#
# Every target that touches secrets routes through `doppler run --` so a
# fresh shell with no env exported just works. `doppler.yaml` at the repo
# root pins `project-tracker/dev`. See CLAUDE.md → "Authorization — Check
# Doppler First" for the full token list and what each one is for.
#
# Two project-tracker-specific wrinkles the reference pattern does not have:
#
#   1. `./pt` runs WITHOUT Doppler; nothing in it needs a secret. Targets that
#      shell out to `./pt` carry no `doppler run --` prefix.
#   2. Not every secret lives in `project-tracker/dev`. The alert digest runs
#      itself under the Doppler project named in the local config's [digest]
#      section (scripts/config.py), so its targets carry no `doppler run --`.
#
# Targets are tab-indented (Makefile-required) and intentionally trivial —
# they exist as muscle-memory shortcuts, not abstractions over the
# underlying commands. If a target stops working, run the body directly.
#
# PYTHON resolves to the repo venv, and finds it from a git worktree too. It
# backs the server/launch targets below. `test`/`test-fast` deliberately do NOT
# use it: they run `uv run --extra test --python 3.13 python -m pytest` so make
# runs exactly what CI runs — and a bare worktree `.venv` (which a governance
# hook creates without pytest) cannot shadow it.
#
# It must be `.venv/`, not `venv/`. This repo runs on uv, and `uv run --project`
# (which scripts/launch-dashboard.sh uses to serve the dashboard) creates and
# uses `.venv/` by convention. `make` pointing at `venv/` meant the test suite
# and production were two different interpreters -- they drifted across a
# STARLETTE MAJOR VERSION (0.41 vs 1.0.0) before anyone noticed, so 883 green
# tests were measured against a framework production was not running (#6999).
# Whatever else changes here, the interpreter make uses and the interpreter
# uv resolves must stay the same one.
#
# A worktree has no .venv/ of its own, so a bare `.venv/bin/python` simply did
# not exist there and every PYTHON-based target failed on sight. The override
# was documented, but three separate agents still tripped over it in one day --
# and worktrees are the sanctioned way to do risky work (editing
# ~/.claude/hooks/ in place blocks every concurrent agent's Bash calls the
# moment the file is briefly unparseable). A tax on the safe workflow gets
# paid in people not using it.
#
# --git-common-dir points at the MAIN checkout's .git even from a worktree (a
# worktree's own .git is a text pointer file, so --git-dir would give the wrong
# answer), and its parent is the main worktree root. Outside a repo both lookups
# miss and PYTHON falls back to python3, which fails loudly on a missing import.
#
# Resolved entirely inside one $(shell), NOT with $(abspath)/$(patsubst): those
# operate on whitespace-separated word lists, so a checkout path containing a
# space is shredded into fragments, the venv lookup misses, and make silently
# falls back to python3 -- breaking the very worktree case this exists to fix,
# with no error. Shell quoting handles spaces; Make's word functions cannot.
# Every use site quotes "$(PYTHON)" for the same reason.
PYTHON ?= $(shell \
	if [ -x .venv/bin/python ]; then printf '%s' .venv/bin/python; \
	else \
	  common=$$(git rev-parse --git-common-dir 2>/dev/null) && \
	  root=$$(cd "$$common/.." 2>/dev/null && pwd) && \
	  [ -x "$$root/.venv/bin/python" ] && printf '%s' "$$root/.venv/bin/python" \
	  || printf '%s' python3; \
	fi)
# test/test-fast run exactly what CI runs. Override only if you need a custom
# pytest invocation; the rest of the targets keep using $(PYTHON).
PYTEST_CMD ?= uv run --extra test --python 3.13 python -m pytest
HOST ?= 127.0.0.1
PORT ?= 8000

FRONTEND ?= dashboard/frontend

.PHONY: help test test-fast dashboard dashboard-restart dashboard-stop \
        dashboard-health digest digest-dry backup backup-status \
        doppler-check schema \
        frontend-deps test-frontend lint-frontend build-frontend

help:  ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# The frontend has the same worktree problem PYTHON has, for a different reason.
# A git worktree gets no $(FRONTEND)/node_modules, so `npm run test` dies with
# "sh: vitest: command not found". Pointing at the main checkout's binary does not
# help either: Node resolves packages relative to the config file, so vite.config.ts
# then fails with ERR_MODULE_NOT_FOUND 'vitest'. A symlink is the only thing that
# actually works, and until this target existed every agent had to rediscover that
# for themselves -- a tax on worktrees, which are the sanctioned way to do
# concurrent work here. Same argument that justified the PYTHON block above (#6992).
#
# node_modules is gitignored, so the symlink never shows up in git status.
frontend-deps:  ## Ensure the frontend's node_modules resolves (symlinks it in a worktree).
	@if [ -e "$(FRONTEND)/node_modules" ]; then \
	  exit 0; \
	fi; \
	common=$$(git rev-parse --git-common-dir 2>/dev/null); \
	root=$$(cd "$$common/.." 2>/dev/null && pwd); \
	if [ -n "$$root" ] && [ -d "$$root/$(FRONTEND)/node_modules" ]; then \
	  ln -s "$$root/$(FRONTEND)/node_modules" "$(FRONTEND)/node_modules" && \
	  echo "linked $(FRONTEND)/node_modules -> $$root/$(FRONTEND)/node_modules"; \
	else \
	  echo "No node_modules in this checkout or the main one." >&2; \
	  echo "Run: cd $(FRONTEND) && npm install" >&2; \
	  exit 1; \
	fi

test-frontend: frontend-deps  ## Frontend unit tests (vitest). Works from a worktree.
	cd "$(FRONTEND)" && npm run test

lint-frontend: frontend-deps  ## Frontend lint (eslint). Works from a worktree.
	cd "$(FRONTEND)" && npm run lint

build-frontend: frontend-deps  ## Frontend typecheck + build (tsc -b && vite build).
	cd "$(FRONTEND)" && npm run build

test:  ## Full pytest suite (no secrets needed — tests run offline).
	$(PYTEST_CMD) tests/ -q

test-fast:  ## Same suite, stop at the first failure.
	$(PYTEST_CMD) tests/ -q -x --no-header

schema:  ## Regenerate scripts/db/schema.sql after adding a migration (tests fail until you do).
	$(PYTHON) scripts/db/schema_dump.py --write

dashboard:  ## Launch the dashboard in the foreground.
	./scripts/launch-dashboard.sh

dashboard-restart:  ## Kill and relaunch the dashboard in the background on $(HOST):$(PORT).
	-pkill -f "uvicorn dashboard.app"
	sleep 1
	"$(PYTHON)" -m uvicorn dashboard.app:app --host $(HOST) --port $(PORT) &

dashboard-stop:  ## Stop the running dashboard.
	-pkill -f "uvicorn dashboard.app"

dashboard-health:  ## Curl the dashboard health endpoint.
	@curl -sf http://$(HOST):$(PORT)/api/health || \
	  { echo "dashboard not responding on $(HOST):$(PORT)"; exit 1; }

digest-dry:  ## Render the portfolio alert digest to stdout without sending.
	./scripts/alert-digest.sh --dry-run

digest:  ## Send the portfolio alert digest email (secrets per the local config's [digest]).
	./scripts/alert-digest.sh

backup:  ## Atomic point-in-time snapshot of tracker.db (no secrets needed).
	./scripts/backup-db.sh

backup-status:  ## Backup + off-machine backup health.
	./pt backup status

doppler-check:  ## Verify Doppler auth by listing secret NAMES (never values).
	doppler secrets --project project-tracker --config dev --only-names
