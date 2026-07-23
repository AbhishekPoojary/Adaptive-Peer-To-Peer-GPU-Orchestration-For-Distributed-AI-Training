# Dashboard

The operator dashboard for the GPU orchestrator (M3.5): fleet health, node
detail, job lifecycle, and job submission against the real orchestrator API.
No fake data anywhere — every view renders exactly what the API returns,
including its absence (see the repo's `CONTRIBUTING.md`).

Stack, routing, and API-client decisions are recorded in
[`docs/adr/ADR-011-dashboard-stack.md`](../docs/adr/ADR-011-dashboard-stack.md).

## Prerequisites

The orchestrator stack must be running (`docker compose up` from the repo
root) and reachable at `http://localhost:8090` — `curl http://localhost:8090/health`
should return `{"status":"ok","db":"ok"}`.

## Development

```bash
npm install
npm run dev
```

Vite's dev server proxies everything under `/api` to the orchestrator
(`vite.config.ts`), so the app talks to `http://localhost:5173/api/...` and
never needs CORS configured on the backend. Open `http://localhost:5173`.

## Regenerating the typed API client

The client (`src/api/schema.gen.ts`) is generated from the orchestrator's
live OpenAPI schema, not hand-typed. Regenerate it after any backend schema
change (with the stack running):

```bash
npm run generate:api
```

## Other scripts

```bash
npm run build     # tsc -b && vite build
npm run lint      # eslint .
npm run preview   # preview the production build
```
