# Project Tracker frontend

This directory contains the React + TypeScript + Vite frontend for the SPA-owned app views:

- `/kanban`
- `/kanban/:project`
- `/agentic`

The built assets are served by the FastAPI backend in `dashboard/app.py`.

## Node version

Vite 7 requires a newer Node runtime than the repo's older local defaults.

- Supported: `^20.19.0 || >=22.12.0`
- Recommended local default: Node `22` via `.nvmrc`

If you use `nvm`:

```bash
cd dashboard/frontend
nvm use
```

## Common commands

```bash
cd dashboard/frontend
npm install
npm run dev
npm run build
npm run lint
```

## Notes

- `npm run dev` starts the Vite dev server for frontend work.
- `npm run build` produces `dist/`, which the backend serves for SPA routes.
- `npm run lint` validates the frontend TypeScript/React code.
