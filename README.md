# agent-kanban
<img width="1718" height="853" alt="Screenshot 2026-11-08 at 4 56 27 PM" src="https://github.com/user-attachments/assets/bb562c06-dea3-45f3-8d1f-68dfb12edd98" />

A local Kanban board, project scanner and dashboard for a folder full of projects. One SQLite database holds the board; a CLI (`pt`) reads and writes it, and a FastAPI + React dashboard shows it.

**Built for one person running many personal projects with coding agents, not for teams.** There are no user accounts or permissions: one owner and their agents share one board.

**Agent-first, from the first commit.** The point of `pt` is that a coding agent gets what it needs without hunting through files. Instructions that used to live in Markdown docs moved into the CLI itself: `pt --help` and each command's `--help` are the documentation, and `pt tasks`, `pt info` and `pt doctor --json` give an agent its work, its context and a health check in one call each. The dashboard is for the person watching.

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

## Commands

Every group has `--help` with examples; most read commands take `--json`.

**Set up and find your projects**

- `pt init`: create the database (`data/tracker.db`). Run it once, before anything else.
- `pt scan`: walk the projects folder and record every project in it. Run it after you add, move or delete projects.
- `pt sync-project <name>`: re-read one project. Faster than a full scan when only one changed.
- `pt list` and `pt status <name>`: every project at a glance, or one in detail.
- `pt project`: add a project without scanning, or mark one as checked out to a machine, so an agent on another machine leaves it alone.
- `pt retire-project <name>`: retire a project: move its folder to the trash, remove its cards, and list what still mentions it. It only prints the plan until you pass `--execute`.

**Run the board**

- `pt tasks`: the Kanban board. Create a card, then `start`, `review` and `done` it as work moves; `pt tasks next` gives an agent the next card to take.
- `pt launch`: open the dashboard at http://127.0.0.1:8000, the same board with drag and drop.
- `pt handoff`: leave a structured note about unfinished work for the next session, and mark it resolved when someone picks it up.

**Keep reference data in one place**

- `pt info`: per-project facts an agent needs and shouldn't guess: hosts, paths, which secret lives where (names only).
- `pt reminders`: one-line things worth remembering (a tool, a guide, a decision), shown on the dashboard's Reminders page.
- `pt calendar`: dated events and recurring jobs, so agents can see what is due or overdue.
- `pt jobs import`: load job listings from a JSONL file into the dashboard's Jobs page.

**Housekeeping**

- `pt hygiene`: read-only check of every project's git state (uncommitted work, local-only or unpushed branches, stashes).
- `pt worktrees`: list and clean the `.claude/worktrees/` that sub-agents leave behind.
- `pt size`: lines of code per project, scored against a baseline, to see whether a cleanup actually shrank anything.
- `pt record-run`: record a scheduled job's start and exit code (`scripts/record-run.sh` wraps a job with it), so failed runs show up.
- `pt migration`: record a bulk operation's start, finish and manifest, so a large change can be audited later.

**Look after the install**

- `pt backup`: check that backups are recent, take one, or restore from one.
- `pt db migrate`: apply schema migrations after an update.
- `pt config show --effective`: what is configured, and from where.
- `pt addons`: list the optional features in `addons/` (Reminders and Codebase size so far), and turn one off or back on; turning one off keeps its data.
- `pt doctor`: check whether this install works for a non-interactive caller such as an agent (`--json` for scripts).

## Optional integrations

Anything beyond the board is off until you turn it on in a private config file, `~/.config/project-tracker/config.toml` (or `$PT_CONFIG`). A section that is absent turns its integration off, and the commands that need it say so. A section that is wrong is an error naming the file. Every key is documented under "Local integrations" in [`scripts/config.py`](scripts/config.py).

- **`[chat]`, used daily.** `pt message` is a client for an Agent Chat server. The server's URL and key come from `~/.claude/agent-chat.env` or the environment. The section only adds recipient addresses beyond your projects.
- **`[digest]`, used daily.** A daily alert email (`scripts/alert_digest.py`) listing failed scheduled jobs and due or missed calendar events. It needs an email-sending account.
- **`[jobs]`, used weekly.** House rules for the Jobs page's tailor-resume prompt.

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
