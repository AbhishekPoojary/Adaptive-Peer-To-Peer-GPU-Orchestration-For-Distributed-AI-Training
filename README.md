# gpu-orchestrator

Decentralized, peer-to-peer GPU orchestration for distributed AI training.

Peer laptops and workstations — mixed GPU and CPU, behind different NATs —
enroll as agents, dial out to a central orchestrator, and **pull** training work
via lease-based assignment. Jobs are real PyTorch training runs on real
datasets, executed in isolated containers on whichever peers are healthy enough
to take them.

Every architectural choice is recorded in `docs/adr/`.

## Status

**M0–M10 complete.** The system runs end to end: a peer enrolls with one
command, heartbeats real hardware telemetry, claims a lease, trains a real model
in a container, streams its logs and metrics live to a dashboard, checkpoints to
object storage, and recovers onto another peer when a node dies mid-run.

What is measured rather than asserted:

- Real MNIST training to **99.03%** test accuracy on CUDA, driven end to end
  through the authenticated API.
- The adaptive scheduler places **6/6** jobs on a reliable node over one with
  three recorded failures, where `round_robin` manages 3/6 and `least_loaded`
  2/6 — see `bench/report/`.
- A peer SIGKILLed mid-training is detected in **5.8 s** and its job completes
  on another machine **55.9 s** after the peer vanished.
- 279 tests against a real Postgres. No mocked database, no simulated failures.

`docs/STATUS.md` is the honest account of what is done, what is explicitly not
claimed, and where to start next.

What is **not** claimed: any throughput speedup from distribution. All
development happened on one laptop with one GPU, where extra ranks contend for
the same device — M5 measured `world_size=2` at 251 s against `world_size=1`'s
171 s. Distribution is a cost on one machine, and every benchmark artifact
carries a machine-written `limitations` block saying which score terms its run
could and could not exercise (ADR-013).

## How it works

| Concern | Approach | ADR |
| --- | --- | --- |
| Assignment | Agents **pull** leases; the orchestrator never dials in, so peers work from behind NAT with no port forwarding | ADR-003 |
| Fencing | Monotonic `lease_epoch` per attempt; a stale holder's writes are rejected | ADR-003 |
| Failure detection | φ-accrual detector over real heartbeat inter-arrivals, with a 5 s hard floor | ADR-004 |
| Placement | `S_i = α·L_i − β·R_i + γ·D_i` over measured load, earned reliability, and measured RTT | ADR-009 |
| Reliability | Wilson lower bound over recorded lease outcomes with time decay — never assigned | ADR-009 |
| Distributed training | `torchrun` + c10d rendezvous, `gloo` backend, real DDP | ADR-005 |
| Checkpoints | Atomic blob-then-manifest writes to MinIO; resume on reassignment | ADR-006 |
| Isolation | `cap_drop=ALL`, no-new-privileges, read-only rootfs, memory/pid limits | ADR-007 |
| Machine identity | One-time enrollment token → Ed25519 challenge-response → short-lived JWT | ADR-008 |
| Human identity | Password → scrypt → short-lived JWT with a separate audience and role | ADR-012 |

## Layout

- `orchestrator/` — FastAPI control plane (async SQLAlchemy 2.0, Postgres 16,
  Alembic).
- `agent/` — runs on each peer: telemetry, lease lifecycle, container execution.
- `trainer/` — the real training entrypoint (`torchrun`/DDP aware).
- `dashboard/` — React + Vite operator UI with live logs and metrics.
- `installer/` — one-command agent install.
- `bench/` — evaluation harness and machine-written reports
  (`bench/schema.json` documents the artifact shape).
- `scripts/` — operational commands (`create_user.py`, data repair, guardrail).
- `tests/` — pytest suite; the only place `Fake*`/`Stub*` doubles may live.
- `deploy/` — `compose.yaml` + `.env.example` for the dev stack.
- `docs/adr/` — architecture decision records (ADR-001..013, plus addenda).

## Quickstart

### 1. Bring up the control plane

```bash
cp deploy/.env.example deploy/.env
docker compose -f deploy/compose.yaml up -d --build
curl -f http://localhost:8090/health
```

### 2. Create your account

Every human-facing endpoint requires an authenticated user (ADR-012), so a fresh
stack has no way in until you make one. There is no self-registration: on this
system an account is permission to run containers on other people's machines,
so accounts are created by whoever already has shell access to the orchestrator
host.

```bash
export DATABASE_URL=postgresql+asyncpg://orchestrator:orchestrator@localhost:5433/orchestrator
python -m scripts.create_user --username <you> --role ADMIN
```

It prompts for the password, or reads `ORCH_USER_PASSWORD`. It never accepts one
as a command-line argument, where it would land in shell history and the process
table. Use `--role OPERATOR` for someone who should submit and watch jobs but
not enroll machines into the fleet.

### 3. Add a peer

Sign in to the dashboard and use **Add a node**, which mints a single-use
enrollment token and shows a one-line install command to run on the peer
machine. The dashboard then waits for that exact node to appear.

Or from the CLI:

```bash
TOKEN=$(curl -sX POST http://localhost:8090/auth/enrollment-tokens \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"created_by":"me"}' | python -c 'import json,sys;print(json.load(sys.stdin)["token"])')

python -m agent --orchestrator http://localhost:8090 --enrollment-token "$TOKEN"
```

The agent generates its own keypair on first run; the private key never leaves
the machine.

### 4. Submit training

Use the dashboard's **Submit** page, or:

```bash
curl -sX POST http://localhost:8090/jobs \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"spec":{"dataset":"mnist","model":"cnn","epochs":3,"batch_size":64,
       "learning_rate":0.01,"world_size":1,"min_gpu_mem_bytes":null},
       "scheduler_name":"adaptive"}'
```

Attribution comes from your token, not the request body — `submitted_by` is
evidence, not a self-declared string.

## Development

```bash
pip install -e ".[dev]"

# The suite needs a real Postgres; the compose stack provides one.
export TEST_DATABASE_URL=postgresql+asyncpg://orchestrator:orchestrator@localhost:5433/orchestrator
pytest -q

ruff check .
mypy orchestrator agent bench
bash scripts/check_no_fake_data.sh
```

There is no SQLite substitute: the concurrency guarantees under test are
Postgres row-locking semantics (`SELECT … FOR UPDATE SKIP LOCKED`), which
SQLite does not share.

## Benchmarks

```bash
export BENCH_PASSWORD='<your password>'
python -m bench.harness --scenario reliability_placement --username <you>
python -m bench.harness --scenario failure_recovery --username <you>
```

The harness starts real agent processes, induces real failures (`docker kill` on
a running trainer; `SIGKILL` on a peer that must then be detected as gone), and
writes a timestamped artifact to `bench/report/` carrying the git SHA and the
hardware it ran on. It refuses to run from a dirty worktree, and a run that
cannot complete its measurements writes **nothing** rather than publishing a
report with a hole in it.

Runs take minutes: they include real training and real failure detection, which
has a 5 s floor by design (ADR-004). See `docs/OPERATIONS.md`.

## Ground rules

Every number this project reports is measured. No fabricated telemetry, no
assumed reliability, no `time.sleep` standing in for work, no simulated
failures outside `tests/`. `scripts/check_no_fake_data.sh` enforces what it
mechanically can and runs in CI.

See `CONTRIBUTING.md` for the full rules and the reasoning behind them.
