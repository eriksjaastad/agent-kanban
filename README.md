# project-tracker
<img width="1718" height="853" alt="Screenshot 2026-11-08 at 4 56 27 PM" src="https://github.com/user-attachments/assets/bb562c06-dea3-45f3-8d1f-68dfb12edd98" />

A local Kanban board, project scanner and dashboard for a folder full of projects, built to be driven by AI coding agents as much as by people. One SQLite database holds the board; a CLI (`pt`) reads and writes it, and a FastAPI + React dashboard shows it.

It runs on the machine it lives on. Nothing is hosted and no account is needed.

## What it does
<img width="600" height="650" alt="Screenshot 2026-11-08 at 5 00 23 PM" src="https://github.com/user-attachments/assets/fafaf5a5-34b2-437a-904e-f99364f30ad6" />

- **Board.** Cards move Backlog → To Do → In Progress → Review → Done, per project (`pt tasks`). Everything reads and writes through the CLI or the dashboard's API, and destructive operations back up first.
- **Projects.** `pt scan` finds the projects in your projects folder (any directory with a `.git`, a README or a build file) and records them. The dashboard shows their cards, GitHub activity and scheduled jobs.
- **Reference data.** `pt info` keeps per-project notes: hosts, paths, and which secret lives where (names, never values).
- **Small local tools.** Reminders, a calendar and job listings, each with a page and a command (`pt reminders`, `pt calendar`, `pt jobs`).
- **Agent-friendly.** Every command has `--help`, most read commands take `--json`, and `pt doctor --json` says whether a non-interactive caller can use this install.

## Requirements

- macOS (the scheduled jobs are launchd agents; Linux is untested), Python 3.13 and [uv](https://docs.astral.sh/uv/)
- Node 20.19+ or 22.12+ to build the dashboard's frontend
- Optional: `gh` for the GitHub panels

## Quick start

```bash
cd dashboard/frontend && npm ci && cd ../..
make build-frontend
./pt init                       # create data/tracker.db
./pt scan                       # find the projects in ~/projects
./pt launch                     # dashboard at http://127.0.0.1:8000
./pt tasks                      # the board in the terminal
```

`pt` runs itself through uv, so the first call installs its Python dependencies. Set `PROJECTS_ROOT` to scan a folder other than `~/projects`.

## Optional integrations

Anything beyond the board is off until you turn it on in a private config file, `~/.config/project-tracker/config.toml` (or `$PT_CONFIG`). A section that is absent turns its integration off, and the commands that need it say so. A section that is wrong is an error naming the file. Every key is documented under "Local integrations" in [`scripts/config.py`](scripts/config.py):

| Section | Turns on |
|---|---|
| `[memory]` | `pt memory` and `pt graph`, over a separate memory store (a `brain.py` and its `brain.db`) |
| `[chat]` | more recipients for `pt message`, which talks to an Agent Chat server |
| `[digest]` | a daily alert email (`scripts/alert_digest.py`) |
| `[morning]` | the dashboard's Morning page, read from a plan README |
| `[jobs]` | house rules for the Jobs page's tailor-resume prompt |

`pt config show --effective` prints what is configured, and `pt doctor` checks it.

## Development

```bash
make test             # the Python suite (offline, no secrets)
make test-frontend    # vitest
make lint-frontend    # eslint over dashboard/frontend/src
make build-frontend   # tsc + vite build
make help             # everything else
```

`scripts/local-checks.sh` runs all of these the way CI would. Schema changes are numbered migrations in `scripts/db/migrations/`; run `make schema` after adding one. The visual Kanban tests need a one-time Playwright Chromium install: `uv run --extra test --python 3.13 playwright install chromium`.

The frontend lint covers `src/` only. Lint the vanilla-JS files in `dashboard/static/` from the repo root, so `no-redeclare` sees their shared global scope:

```bash
dashboard/frontend/node_modules/.bin/eslint --no-config-lookup \
  --rule '{"no-redeclare": "error"}' dashboard/static/<file>.js
```
