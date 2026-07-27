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
- `dashboard/` — operator UI; built in M3.5, sign-in added in M8.
- `installer/` — one-command agent install; placeholder.
- `bench/` — benchmark scenarios + machine-written reports
  (`bench/schema.json` documents the artifact shape).
- `tests/` — pytest suite; the only place `Fake*`/`Stub*` doubles may live.
- `deploy/` — `compose.yaml` + `.env.example` for the dev stack.
- `docs/adr/` — architecture decision records (ADR-001..012).

## Quickstart (dev stack)

```bash
cp deploy/.env.example deploy/.env
docker compose -f deploy/compose.yaml up -d --build
curl -f http://localhost:8090/health
```

### Create your operator account (required since M8)

Every human-facing endpoint requires an authenticated user (ADR-012), so a
fresh stack has no way in until you create one. Accounts are made on the
orchestrator host — there is no self-registration, because an account on this
system is permission to run containers on other people's machines:

```bash
export DATABASE_URL=postgresql+asyncpg://orchestrator:orchestrator@localhost:5433/orchestrator
python -m scripts.create_user --username <you> --role ADMIN
```

It prompts for the password (or reads `ORCH_USER_PASSWORD`); it never accepts
one on the command line, where it would land in shell history and the process
table. Use `--role OPERATOR` for someone who should submit and watch jobs but
not enroll machines into the fleet.

Then sign in at the dashboard, or from the CLI:

```bash
curl -sX POST http://localhost:8090/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<you>","password":"<password>"}'
```

## Development

```bash
pip install -e ".[dev]"
pytest -q
bash scripts/check_no_fake_data.sh
```

See `CONTRIBUTING.md` for the anti-fabrication rules that govern this
codebase.
