# gpu-orchestrator

Decentralized, P2P GPU orchestration for adaptive AI training. Peer laptops
and workstations (mixed GPU/CPU) enroll as agents, dial out to a central
orchestrator, and pull training work via lease-based assignment. See
`docs/adr/` for the architecture decisions this implements.

## Status

**M0 — skeleton + guardrails.** The control plane boots, talks to a real
Postgres, and exposes a real `/health` check. Scheduling, leases, agent
telemetry, and distributed training are stubs, filled in by later
milestones. Nothing here fabricates data — see `CONTRIBUTING.md`.

## Layout

- `orchestrator/` — FastAPI control plane (async SQLAlchemy 2.0 + Postgres
  16 + Alembic).
- `agent/` — runs on each peer node; package skeleton in M0.
- `trainer/` — `torchrun`/DDP training entrypoint; placeholder in M0.
- `dashboard/` — operator UI; built in M3.5.
- `installer/` — one-command agent install; placeholder.
- `bench/` — benchmark scenarios + machine-written reports
  (`bench/schema.json` documents the artifact shape).
- `tests/` — pytest suite; the only place `Fake*`/`Stub*` doubles may live.
- `deploy/` — `compose.yaml` + `.env.example` for the dev stack.
- `docs/adr/` — architecture decision records (ADR-001..010).

## Quickstart (dev stack)

```bash
cp deploy/.env.example deploy/.env
docker compose -f deploy/compose.yaml up -d --build
curl -f http://localhost:8000/health
```

## Development

```bash
pip install -e ".[dev]"
pytest -q
bash scripts/check_no_fake_data.sh
```

See `CONTRIBUTING.md` for the anti-fabrication rules that govern this
codebase.
