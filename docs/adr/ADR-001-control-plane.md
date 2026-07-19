# ADR-001: Control plane stack

## Status
Accepted

## Context
The orchestrator needs a control plane that can serve REST + WebSocket
traffic, hold durable state (nodes, jobs, leases, lease epochs) under
concurrent access, and be operable by a small team without adopting a
heavyweight scheduler stack (Kubernetes, Nomad). It must run comfortably on
a single Windows laptop for dev (ADR-010) and scale to a small fleet.

## Decision
Use FastAPI (async) as the HTTP/WebSocket layer, PostgreSQL 16 as the
system of record, SQLAlchemy 2.0's async ORM for data access, and Alembic
for schema migrations. All I/O on the request path is async; blocking
calls (e.g. any future NVML/psutil calls made server-side) are avoided or
offloaded.

## Consequences
- Postgres's `SELECT ... FOR UPDATE SKIP LOCKED` (ADR-003) and row-level
  locking are available for lease claiming without extra infrastructure.
- Alembic migrations run on every container start (see
  `orchestrator/docker-entrypoint.sh`), so schema drift is caught
  immediately rather than discovered later.
- Async SQLAlchemy requires care with session lifetimes across
  `await` boundaries; this is centralized in `orchestrator/core/db.py`.
- No message broker, no separate scheduler service — the tradeoff is
  fewer moving parts in dev at the cost of building lease/heartbeat logic
  ourselves (accepted; see ADR-003, ADR-004).
