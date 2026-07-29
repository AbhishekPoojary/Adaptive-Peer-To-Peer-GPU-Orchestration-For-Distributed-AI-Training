# Operations runbook

Real procedures for running this system, plus the traps that have actually bitten
during development. Everything here has been executed against the live stack.

Ports are non-default because an unrelated project occupies 8000/5432/9000 on
the development machine: **orchestrator 8090, Postgres 5433, MinIO 9010/9011**.
They come from `deploy/.env`.

---

## Migrations

### Run them from the host, not `docker exec`

```bash
export DATABASE_URL=postgresql+asyncpg://orchestrator:orchestrator@localhost:5433/orchestrator
python -m alembic -c orchestrator/alembic.ini upgrade head
```

**The trap:** `docker exec deploy-orchestrator-1 alembic upgrade head` runs the
migrations *baked into the running image*. If the image predates the migration
you just wrote, alembic reports success while doing nothing, and the next
request fails with something misleading like:

```
invalid input value for enum lease_state: "UNCLAIMED"
```

The migration "succeeded" and the column still rejects the value. Run alembic
from the host against the published port, or rebuild the image first.

### ...but then rebuild the image, or the container will not start

The mirror of the trap above, and it is worse because it crash-loops:

```
FAILED: Can't locate revision identified by '0009_unclaimed_backoff'
Container deploy-orchestrator-1  Restarting (255)
```

The database is now at a revision the *image* has never heard of, so the
entrypoint's `alembic upgrade head` cannot build a path from it and the
container dies on boot, forever.

```bash
docker compose -f deploy/compose.yaml build orchestrator
docker compose -f deploy/compose.yaml up -d --force-recreate orchestrator
```

The rule that avoids both traps: **the image must always know at least as many
revisions as the database.** Write a migration, rebuild, then apply.

### Verify a migration is reversible before shipping it

```bash
python -m alembic -c orchestrator/alembic.ini downgrade -1
python -m alembic -c orchestrator/alembic.ini upgrade head
```

A migration that cannot go back is a migration you cannot safely deploy.

---

## Accounts

```bash
export DATABASE_URL=postgresql+asyncpg://orchestrator:orchestrator@localhost:5433/orchestrator

# First admin
python -m scripts.create_user --username <name> --role ADMIN

# Someone who submits and watches jobs but cannot enroll machines
python -m scripts.create_user --username <name> --role OPERATOR

# Reset a forgotten password
python -m scripts.create_user --username <name> --role OPERATOR --update

# Non-interactive (CI, entrypoint)
ORCH_USER_PASSWORD='...' python -m scripts.create_user \
  --username ci --role OPERATOR --no-prompt
```

The password is never accepted as a CLI argument — it would land in shell
history and, on Linux, the world-readable process table.

### Disabling access

Set `disabled_at` on the user row. It takes effect on the **next request**, not
at token expiry: `require_user` re-reads the row every time.

```sql
UPDATE users SET disabled_at = now() WHERE username = '<name>';
```

There is no token revocation list. A stolen token stays valid until it expires
(`USER_ACCESS_TOKEN_TTL_SECONDS`, default 15 min) unless the account is
disabled. That trade-off is recorded in ADR-012.

---

## Enrollment tokens

Tokens are single-use and expiring. To withdraw one before it is used:

```bash
curl -s http://localhost:8090/auth/enrollment-tokens \
  -H "Authorization: Bearer $ADMIN_TOKEN"

curl -sX POST http://localhost:8090/auth/enrollment-tokens/<id>/revoke \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Revoking an already-used token returns **409**, deliberately: the node it
enrolled is already in the fleet, and reporting success would imply an access
removal that did not happen. Remove that node instead.

---

## Docker Desktop is down

Symptom:

```
failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine
```

```powershell
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
```

Then wait for it, rather than guessing:

```bash
until docker info >/dev/null 2>&1; do sleep 5; done
docker compose -f deploy/compose.yaml up -d
```

---

## Reliability data looks wrong

Symptom: a node shows failures it did not earn, and the adaptive scheduler
avoids it for no visible reason.

Reliability is computed straight from `leases` rows, so a misclassified lease
corrupts placement. Before revision `0007`, an offer that was never claimed
(`PENDING` past TTL) was recorded as `EXPIRED` — the same state as a node that
took work and stalled — and counted as a failure. One node accumulated seven
fabricated failures this way.

Inspect and repair:

```bash
# Dry run: reports what would change, writes nothing
python -m scripts.repair_reliability_counts

# Apply
python -m scripts.repair_reliability_counts --apply
```

It reclassifies using the authoritative discriminator — a lease is only ever
claimed via the claim endpoint, which writes a `LEASED` job event carrying that
lease's id, so an `EXPIRED` lease with no such event was never claimed — then
recomputes the counters. Idempotent.

**Do not** hand-edit `lease_success_count` / `lease_failure_count`. They are
derived; the next recompute will overwrite whatever you typed, and in the
meantime the scheduler is ranking on a number nobody can trace.

---

## A job is stuck in REASSIGNED, cycling epochs

Look at the job's event timeline in the dashboard. Rapid epoch churn with
leases expiring unclaimed means the scheduler is offering work to a node whose
agent cannot take it — historically, an agent stuck on an orphaned container
from a cancelled job.

Check what the node is actually running:

```bash
docker ps --filter label=gpu-orchestrator.role=trainer \
  --format '{{.ID}}\t{{.Label "gpu-orchestrator.job_id"}}\t{{.Status}}'
```

Trainer containers carry `gpu-orchestrator.job_id` and `.lease_id` labels, so a
running container can always be traced to the lease that launched it. A
container whose job is `CANCELLED` or long finished is an orphan; the agent
should have stopped it (fixed in M7.1b) and the fact that it did not is worth
investigating rather than just killing.

---

## Watching the system

```bash
curl -s http://localhost:8090/health          # liveness + DB
curl -s http://localhost:8090/metrics         # Prometheus text format
docker logs -f deploy-orchestrator-1
```

The agent exposes its own `/metrics` when started with `--metrics-port`.

---

## Running the benchmarks

```bash
export BENCH_PASSWORD='<password>'
python -m bench.harness --scenario reliability_placement --username <you>
python -m bench.harness --scenario failure_recovery --username <you>
```

Notes:

- The harness **refuses a dirty worktree**, because the artifact's `git_sha`
  would not describe the code that ran. `--allow-dirty` marks the artifact
  `provisional` instead of lying about it.
- Runs take minutes, not seconds: they include real training and real failure
  detection (which has a 5 s floor per ADR-004).
- Agents are started as real subprocesses with temporary state directories.
  `--keep-workdir` preserves their logs for debugging.
- A run that cannot complete its measurements writes **no artifact** and exits
  non-zero. An absent report honestly says "this did not run"; a partial one
  invites averaging over a hole.

---

## Regenerating the dashboard's API types

After any change to the orchestrator's schemas:

```bash
python -c "import json;from orchestrator.main import create_app;\
  open('openapi.json','w').write(json.dumps(create_app().openapi()))"
cd dashboard && npx openapi-typescript ../openapi.json -o src/api/schema.gen.ts
npx tsc --noEmit
```

The typed client is generated, not hand-written, so a backend change that breaks
the frontend shows up as a compile error rather than a runtime `undefined`.

---

## Before deploying anywhere real

- Set `APP_ENV` to something other than `dev`/`test`/`local`. Startup then
  **refuses** to boot with a missing `ADMIN_API_KEY` or the default
  `JWT_SIGNING_KEY` — that check exists so an insecure default cannot reach a
  real deployment quietly.
- `JWT_SIGNING_KEY` signs both node and user tokens; rotating it invalidates
  every session and every agent token at once.
- The rate limiter is in-process. More than one orchestrator replica multiplies
  the effective limit by the replica count (ADR-012 §7). One replica is what
  ADR-010 deploys.
- There is no TLS in this stack. ADR-010 assumes a Tailscale overlay, which
  provides WireGuard transport encryption. Exposing the orchestrator on a public
  interface without TLS would put bearer tokens on the wire in plaintext.
