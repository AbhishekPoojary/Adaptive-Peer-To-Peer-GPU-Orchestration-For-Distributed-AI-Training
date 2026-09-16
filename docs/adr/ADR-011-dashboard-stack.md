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

## Amendment: light visual world, top navigation (2026-09-16)

### What changed

The stack decisions above stand unchanged: React 19, Radix primitives against
our own tokens, Tailwind, TanStack Query owning server state, React Router in
data mode with no loaders. What changed is the visual world this ADR assumed.

- **Dark-first became light.** The tokens are now a light ground with a cool
  cast (`--canvas: #f2f5f9`), white panels, ink text, one accent reserved for
  focus, live measurement and chart lines, and a four-role state palette whose
  `--nosignal` exists so an unmeasured value can be visibly not-a-value.
- **The 220px left sidebar became a top pill row.** `Sidebar.tsx` is deleted;
  `TopNav.tsx` replaces it.
- **Panels lost their borders.** A tinted canvas plus one soft shadow with a
  real offset carries the edge, so the outer border went; hairlines survive
  inside tables and on inputs. Panels do not nest.
- **Geist and Geist Mono are self-hosted** rather than `Inter, system-ui`.

### Why

This ADR's premise was that the audience "runs this while training jobs
execute" — an operator at a console, for whom dark is the right call. PRODUCT.md
records a different primary user: someone with a dataset and no GPU who wants a
trained model back and does not know what a lease is. Writing the use scene out
as one sentence — a person at a desk in a lit room, checking whether strangers'
machines are still working on their model — settles it. A dark console tells
that person they have wandered into somebody's internal tooling.

The sidebar went for a related reason. It spent a fifth of a laptop screen
permanently advertising six destinations the primary user does not need, while
the pages that *are* dense — Jobs, Nodes, the scheduling audit — are tables that
want the width.

### How the direction was chosen

Through the Impeccable direction round (seed `8c98ab03`, scope direction, mode
operate). The roll assigned a distinctive grounded direction; the owner reviewed
the hand and deliberately took the standing exit — the modern product-SaaS
idiom, executed straight at a named craft bar (Stripe, Resend) rather than with
a smuggled quirk. That preference is recorded in PRODUCT.md under Brand
Commitments so future surfaces inherit it instead of reopening the question.
The idiom is evidenced, not guessed: two of the highest-engagement dashboard
shots on Dribbble independently share a tinted-light ground, a top pill nav,
borderless panels and oversized numerals.

The direction contract lives in `.impeccable/surfaces/dashboard-src.md`.

### Consequences

- Legacy token aliases (`--color-base`, `--color-panel`, `--color-primary` …)
  are kept pointing at the new values so no surface renders unstyled. They are
  migration scaffolding, not part of the system.
- `StatTile` is no longer a panel. Every caller already places it inside one,
  and a shadowed box inside a shadowed box reads as two levels of importance
  where there is only one.
- Browser surfaces — selection, caret, scrollbars, focus ring, underline
  offset, tabular numerals — are themed from the palette rather than left at
  browser defaults.
- No dark theme ships. The token layer is authored so one is possible without
  restructuring, but nothing promises it, and claiming a theme that has never
  been rendered would be the same class of error as a fabricated metric.
