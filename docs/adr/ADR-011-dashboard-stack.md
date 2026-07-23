# ADR-011: Dashboard stack

## Status
Accepted

## Context
M3.5 needs a real operator-facing dashboard (`dashboard/`) against the
orchestrator's live REST API (nodes, jobs, leases, scheduling-decision
audit trail) — a control-plane instrument panel this audience runs while
training jobs execute, not a marketing site. It has to be typed end to
end against the actual FastAPI schema (never hand-typed interfaces that
can drift), dark-first per the locked visual spec, and hold to the same
anti-fabrication law as the backend: render exactly what the API
returns, including its absence.

## Decision
- **React + Vite + TypeScript + Tailwind CSS v4.** Vite's React-TS
  template scaffolds the app; Tailwind v4's CSS-first `@theme` block
  maps the locked design tokens (`--bg-base`, `--status-good`, etc.,
  declared as real CSS custom properties in `index.css`) directly onto
  utility classes (`bg-base`, `text-good`, `font-data`) so there is one
  source of truth for the palette, not a duplicated JS config.
- **shadcn/ui-pattern components**, hand-assembled from Radix UI
  primitives (`@radix-ui/react-{dialog,collapsible,tooltip,toast,select,
  label,slot}`) plus `class-variance-authority` — the same copy-into-repo
  convention shadcn/ui itself uses, applied directly against our tokens
  rather than the default shadcn theme, since the visual system here is
  fully locked already.
- **TanStack Query** owns all server state (nodes, jobs, scheduling
  decisions, the submit/cancel mutations) with `refetchInterval` polling
  (no WebSocket exists in the backend yet — REST-polled is a real prior
  decision, not a gap this milestone fills).
- **React Router v7** (`createBrowserRouter`/`RouterProvider`, data
  mode) is used for path-matching and navigation only — no loaders or
  actions, so it never competes with TanStack Query for ownership of
  server state. Chosen over TanStack Router because the app only needs
  five flat destinations plus two detail routes; React Router's
  ecosystem maturity and zero-config `<Link>`/`useParams` won over
  TanStack Router's stricter type-safe routing, which buys more than
  this milestone's shallow route tree needs.
- **Typed API client generated from the live schema**: `openapi-typescript`
  turns `http://localhost:8090/openapi.json` into `src/api/schema.gen.ts`
  (`npm run generate:api`, committed so `npm run build` never needs a
  live server), and `openapi-fetch` is the thin typed fetch wrapper
  (`src/api/client.ts`) built against those generated `paths`/`components`
  types. Nothing is hand-typed against the API surface.
- **Dev-only Vite proxy, not backend CORS.** `vite.config.ts` proxies
  everything under `/api` to `http://localhost:8090` (prefix stripped
  before forwarding; the typed client's `baseUrl` is `/api`), so the
  browser sees same-origin requests and the orchestrator needs zero CORS
  middleware for local dev. This keeps the milestone scoped to
  `dashboard/` with no backend changes. An earlier draft of this proxy
  mapped bare prefixes (`/nodes`, `/jobs`, …) directly — that collided
  with the SPA's own client-side routes of the same name and broke
  refresh-safety on those exact pages (a hard reload of `/nodes` returned
  raw backend JSON instead of the app shell). The single `/api` prefix
  sidesteps the collision because no page route is named `/api/...`.

## Consequences
- Regenerating the client after any backend schema change is one command
  (`npm run generate:api`) against a running orchestrator; drift shows up
  as a `tsc` error, not a silent runtime mismatch.
- The Vite proxy is dev-only. Serving the built dashboard from a
  different origin than the orchestrator in a real deployment will need
  either CORS middleware added to the orchestrator or a reverse proxy
  placing both behind one origin — deliberately deferred; there is no
  deployment story for the dashboard yet (mirrors ADR-010's dev/multi-host
  split, which this doesn't attempt to extend).
- Because React Router owns none of the data layer, every page independently
  decides its own loading/error/empty rendering from TanStack Query's
  state — more boilerplate per page than a loader-based approach, but it
  keeps "never fake a loading/success state" enforceable in one place
  (the query's own `isPending`/`isError`) instead of split across two
  routing-owned and query-owned code paths.
- The sidebar collapses to a hamburger + slide-in drawer below 768px
  (our call for "collapsible on narrow viewports") rather than a bottom
  tab bar, since five destinations read better as a list than as cramped
  icons at 375px.
