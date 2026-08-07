# gpu-orchestrator

**Train AI models on a group of ordinary computers — laptops, desktops, gaming
PCs — working together over the internet.**

Decentralized, peer-to-peer GPU orchestration for distributed AI training.

---

## The problem, in plain words

Training an AI model needs a GPU. GPUs are expensive, and renting them from a
cloud provider costs real money per hour.

Meanwhile, plenty of ordinary machines sit idle — a gaming PC overnight, a lab
computer at the weekend, a friend's laptop. Together they hold a lot of unused
compute. The reason people don't pool it is that doing so is genuinely hard:

- **Home machines are unreachable from the internet.** They sit behind a home
  router (NAT), so you cannot simply connect *to* them.
- **They come and go.** Someone shuts a lid, unplugs a cable, or loses Wi-Fi
  in the middle of your training run.
- **They are not equal.** One has a fast GPU, one has a slow one, one has none
  at all. Some are reliable; some crash constantly.
- **Running someone else's code is a security risk** — for both sides.

`gpu-orchestrator` handles all four, so a pool of ordinary machines can be used
for real training runs.

## How it solves it

The trick is that **the machines call us; we never call them.**

A small program called an **agent** runs on each volunteer machine. It dials
*out* to a central **orchestrator** and asks, "got any work for me?" Because the
connection is always outbound, the home router is happy and no port forwarding,
public IP, or firewall change is ever needed.

```mermaid
flowchart LR
    subgraph Yours
        O["Orchestrator<br/>(decides who trains what)"]
        DB[("Postgres<br/>jobs + nodes")]
        S3[("MinIO<br/>checkpoints")]
        O --- DB
        O --- S3
    end

    A1["Agent<br/>gaming PC, RTX GPU"] -->|"1. I'm alive + my stats"| O
    A2["Agent<br/>laptop, no GPU"] -->|"1. I'm alive + my stats"| O
    A3["Agent<br/>lab desktop, GPU"] -->|"1. I'm alive + my stats"| O

    O -.->|"2. offers a lease"| A1
    A1 -->|"3. trains, streams logs, saves checkpoints"| O
```

1. **Every agent reports in** every few seconds with its real hardware stats —
   GPU memory, load, and how fast it can reach the orchestrator.
2. **The orchestrator picks a machine** for each job and offers it a *lease* —
   a time-limited claim on that work.
3. **The agent trains**, streaming logs and metrics back live, and saving
   progress (*checkpoints*) to shared storage.

If a machine dies mid-run, the orchestrator notices the missing heartbeats,
declares it gone, and hands the job to another machine — which **resumes from
the last checkpoint** instead of starting over.

## Words you'll see in this repo

New to distributed systems? These are the only terms you really need.

| Term | What it means here |
| --- | --- |
| **Orchestrator** | The central brain. Tracks machines, decides who trains what. One per fleet. |
| **Agent** | The small program on each volunteer machine. Reports status, runs training. |
| **Node / peer** | A volunteer machine running an agent. |
| **Job** | One training run you asked for (e.g. "train a CNN on MNIST for 3 epochs"). |
| **Lease** | A time-limited claim on a job. Must be renewed, or it expires and the job is reassigned. Stops two machines doing the same work. |
| **Heartbeat** | The agent's periodic "I'm still alive" message. Silence means trouble. |
| **Checkpoint** | A saved snapshot of a half-trained model, so a crash doesn't lose the work. |
| **NAT** | Why your home PC can't be reached from the internet. Solved here by only ever dialing out. |
| **ADR** | *Architecture Decision Record.* A short doc in `docs/adr/` explaining **why** a choice was made. |

## What it looks like

| Fleet overview | Submitting a job |
| --- | --- |
| ![Fleet overview](docs/screenshots/overview-1440.png) | ![Submit a job](docs/screenshots/submit-1440.png) |

| Node detail | Live job detail |
| --- | --- |
| ![Nodes](docs/screenshots/nodes-1440.png) | ![Job detail](docs/screenshots/job-detail-1440.png) |

---

## Quickstart

You'll do three things: **start the orchestrator**, **make an account**, then
**connect a machine**.

### What you need

| To run the orchestrator | To volunteer a machine |
| --- | --- |
| Docker + Docker Compose | **Python 3.11–3.13** (that's the only hard requirement) |
| Node.js 18+ (for the dashboard) | Docker — *optional*, but gives full isolation |
| | An NVIDIA GPU — *optional*; a CPU-only machine enrolls honestly as a CPU node |

### 1. Start the control plane

```bash
git clone https://github.com/AbhishekPoojary/Adaptive-Peer-To-Peer-GPU-Orchestration-For-Distributed-AI-Training.git
cd Adaptive-Peer-To-Peer-GPU-Orchestration-For-Distributed-AI-Training
cp deploy/.env.example deploy/.env
```

> **⚠️ Set the port before you continue.** Open `deploy/.env` and set:
>
> ```
> ORCHESTRATOR_PORT=8090
> ```
>
> The dashboard's dev server proxies to `http://localhost:8090`, and that value
> is hardcoded in `dashboard/vite.config.ts`. `.env.example` currently ships
> `8000`, which will leave the dashboard unable to reach the API. Everything
> below assumes **8090**.

Now bring it up:

```bash
docker compose -f deploy/compose.yaml up -d --build
curl -f http://localhost:8090/health
```

You should see `{"status":"ok","db":"ok"}`. That starts three services:
Postgres (the database), MinIO (checkpoint storage), and the orchestrator API.

<details>
<summary><b>It didn't work?</b></summary>

- **`curl` fails / connection refused** — the containers may still be building.
  Check with `docker compose -f deploy/compose.yaml logs -f orchestrator`.
- **`{"status":"degraded","db":"down"}` (HTTP 503)** — the API is up but can't
  reach Postgres. Wait a few seconds and retry; the health endpoint reports the
  database honestly rather than pretending it's fine.
- **Port already in use** — change `ORCHESTRATOR_PORT` in `deploy/.env`, but
  then also update the hardcoded URL in `dashboard/vite.config.ts` to match.

</details>

### 2. Create your account

Every human-facing endpoint requires a real account, so a fresh stack has no way
in until you make one. There is **no self-registration** — on this system an
account is permission to run containers on other people's machines, so accounts
are created by whoever has shell access to the orchestrator host.

```bash
pip install -e .
export DATABASE_URL=postgresql+asyncpg://orchestrator:orchestrator@localhost:5432/orchestrator
python -m scripts.create_user --username <you> --role ADMIN
```

It prompts for a password (or reads `ORCH_USER_PASSWORD`). It deliberately
**never** takes a password as a command-line argument, where it would land in
your shell history and the process table.

- `--role ADMIN` — can also enroll new machines into the fleet.
- `--role OPERATOR` — can submit and watch jobs, but not enroll machines.

> If you changed `POSTGRES_PORT` in `deploy/.env`, use that port in
> `DATABASE_URL` instead of `5432`.

### 3. Open the dashboard

```bash
cd dashboard
npm install
npm run dev
```

Open **http://localhost:5173** and sign in with the account from step 2.

### 4. Connect a machine

In the dashboard, click **Add a node**. It creates a single-use enrollment token
and shows you the exact command to run on the volunteer machine. The dashboard
then waits for that specific machine to appear.

**Windows** (native PowerShell — WSL2 not required):

```powershell
$env:ORCH_TOKEN='<TOKEN>'; irm http://<orchestrator>:8090/install.ps1 | iex
```

**Linux / macOS:**

```bash
curl -sSL http://<orchestrator>:8090/install.sh | bash -s -- --token <TOKEN>
```

What happens on that machine:

- If it **has Docker**, training runs inside a locked-down container.
- If it **doesn't**, the installer offers to run training as a normal
  background process instead. It spells out exactly what protection you give up
  and requires you to type *yes*.
- The agent generates its own cryptographic keypair on first run. **The private
  key never leaves the machine.**

> **📡 Machines on other networks.** A friend's laptop cannot reach your
> `localhost`. Put the orchestrator on a [Tailscale](https://tailscale.com)
> network or behind a tunnel, and give peers that address instead. Agents only
> ever dial *out*, so **no peer needs port forwarding or a public IP** — but the
> orchestrator itself does need to be reachable.

### 5. Train something

Use the dashboard's **Submit** page, or the API directly:

```bash
curl -sX POST http://localhost:8090/jobs \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"spec":{"dataset":"mnist","model":"cnn","epochs":3,"batch_size":64,
       "learning_rate":0.01,"world_size":1,"min_gpu_mem_bytes":null},
       "scheduler_name":"adaptive"}'
```

Watch it run on the job detail page: live logs, live loss and accuracy.

Who submitted a job comes from your **token**, never from the request body —
`submitted_by` is evidence, not a self-declared string.

---

## Results — and what is *not* claimed

Every number here was measured on real runs, not estimated. The artifacts live
in `bench/report/` and each carries the git commit and the hardware it ran on.

| Measured | Result |
| --- | --- |
| Real MNIST training, end to end through the authenticated API | **99.03%** test accuracy on CUDA |
| Adaptive scheduler vs. a node with 3 recorded failures | placed **6/6** jobs on the reliable node (`round_robin` 3/6, `least_loaded` 2/6) |
| Machine `SIGKILL`ed mid-training | detected in **5.8 s**; job finished elsewhere **55.9 s** after it vanished |
| A machine with **no Docker at all** | trained to **96.67%** on CUDA via the opt-in unsandboxed path |
| Test suite | ~300 tests against a real Postgres — no mocked database, no simulated failures |

**Not claimed: any speedup from distribution.** All development happened on one
laptop with one GPU, where extra workers fight over the same device — measured,
`world_size=2` took **251 s** against `world_size=1`'s **171 s**. On a single
machine, distribution is a *cost*. Every benchmark artifact carries a
machine-written `limitations` block stating which claims its run could and could
not test (ADR-013).

`docs/STATUS.md` is the honest account of what's done, what isn't, and where to
pick up next.

---

## How it works under the hood

Each row links to the ADR explaining *why* — worth reading if a choice looks odd.

| Concern | Approach | ADR |
| --- | --- | --- |
| Assignment | Agents **pull** leases; the orchestrator never dials in, so peers work from behind NAT with no port forwarding | ADR-003 |
| Fencing | A counter (`lease_epoch`) rises each attempt, so a stale machine waking up late has its writes rejected | ADR-003 |
| Failure detection | φ-accrual detector over real heartbeat timings, with a 5 s hard floor | ADR-004 |
| Placement | `S_i = α·L_i − β·R_i + γ·D_i` over measured load, earned reliability, and measured round-trip time | ADR-009 |
| Reliability | Wilson lower bound over recorded outcomes, decaying over time — earned from history, never assumed | ADR-009 |
| Distributed training | `torchrun` + c10d rendezvous, `gloo` backend, real DDP | ADR-005 |
| Checkpoints | Blob-then-manifest writes to MinIO, so a half-written checkpoint is never resumed from | ADR-006 |
| Isolation | `cap_drop=ALL`, no-new-privileges, read-only rootfs, memory/PID limits; opt-in subprocess path for peers without Docker | ADR-007 |
| Machine identity | One-time token → Ed25519 challenge-response → short-lived JWT | ADR-008 |
| Human identity | Password → scrypt → short-lived JWT with a separate audience and role | ADR-012 |

## Repository layout

```
orchestrator/   FastAPI control plane (async SQLAlchemy 2.0, Postgres 16, Alembic)
agent/          Runs on each peer: telemetry, lease lifecycle, container execution
trainer/        The actual PyTorch training entrypoint (torchrun/DDP aware)
dashboard/      React + Vite operator UI, live logs and metrics
installer/      One-command agent install (install.ps1 / install.sh)
bench/          Evaluation harness + machine-written reports (schema.json documents them)
scripts/        Operational commands (create_user.py, data repair, CI guardrail)
tests/          pytest suite — the only place Fake*/Stub* doubles may live
deploy/         compose.yaml + .env.example for the dev stack
docs/adr/       Architecture Decision Records (ADR-001..013, plus addenda)
docs/STATUS.md  Honest current state and handover notes
```

## Development

```bash
pip install -e ".[dev]"

# The suite needs a real Postgres; the compose stack provides one.
export TEST_DATABASE_URL=postgresql+asyncpg://orchestrator:orchestrator@localhost:5432/orchestrator
pytest -q

ruff check .
mypy orchestrator agent bench
bash scripts/check_no_fake_data.sh
```

There is deliberately **no SQLite option**. The guarantees under test are
Postgres row-locking semantics (`SELECT … FOR UPDATE SKIP LOCKED`), which SQLite
does not share — passing against SQLite would prove nothing.

## Benchmarks

```bash
export BENCH_PASSWORD='<your password>'
python -m bench.harness --scenario reliability_placement --username <you>
python -m bench.harness --scenario failure_recovery --username <you>
```

The harness starts real agent processes and induces real failures — `docker kill`
on a live trainer, `SIGKILL` on a peer that must then be detected as gone. It
refuses to run from a dirty working tree, and a run that can't complete its
measurements writes **nothing** rather than publishing a report with a hole in
it.

Expect minutes per run: real training plus real failure detection, which has a
5 s floor by design (ADR-004). See `docs/OPERATIONS.md`.

## Ground rules

Every number this project reports is measured. No fabricated telemetry, no
assumed reliability, no `time.sleep` standing in for work, no simulated failures
outside `tests/`. `scripts/check_no_fake_data.sh` enforces what it mechanically
can, and runs in CI.

See `CONTRIBUTING.md` for the full rules and the reasoning behind them.
