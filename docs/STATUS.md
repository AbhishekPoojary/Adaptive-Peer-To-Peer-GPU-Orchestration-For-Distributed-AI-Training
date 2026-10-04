# Project status

An honest account of what this system does, what has been measured, and what is
missing. Written for whoever picks it up next — including a future me who has
forgotten the details.

Last updated after the model-download work (ADR-006 addendum 2), which followed
Google sign-in, custom datasets, and admin user management. Deliberately not
pinned to a commit SHA: a document that cites its own commit is stale the moment
it is written.

---

## What works

| Capability | Evidence |
| --- | --- |
| A peer enrolls with one command and appears in the dashboard | `docs/screenshots/`, `installer/` |
| Real training on real data, real backprop, real held-out accuracy | 99.03% MNIST test accuracy, `bench/report/20260728T161648` |
| Lease-based pull assignment survives NAT (agents dial out only) | ADR-003; agents ran with no inbound ports open |
| Epoch fencing rejects a zombie leaseholder's writes | `tests/test_lease_fencing.py` |
| Claim races resolve to exactly one winner | `tests/test_lease_claim_race.py` (real Postgres row locks) |
| φ-accrual detects a vanished peer **within the report's 5 s target** | 4.40 s from SIGKILL to `REASSIGNED`, `bench/report/20261002T200914`. Was 5.8 s under the old 5 s floor and 2.41 s (`20261002T165107`) before the 2 s acceptable pause, which a real internet link proved necessary (ADR-004 addenda 2 and 3). The margin under 5 s is 0.6 s |
| A job survives its node disappearing **within the 15 s recovery target** | Training restarted on the survivor 6.08 s after the kill and had restored the dead peer's checkpoint (step 2900) at 9.53 s; completed at 99.06%. Same artifact |
| **A peer on another network joins over HTTPS with nothing to configure** | `demo.ps1 -Public` gives the orchestrator its own Cloudflare quick tunnel. Verified with a real agent through it: enrolled via `https://`, trained an uploaded 39 MB dataset (downloaded in ranged pieces through the tunnel, digest verified) on its first lease with no false failure, streamed logs and wrote checkpoints over the same link |
| A job survives its *trainer* being killed | Retried on another peer, completed to 99.12% (ADR-005 addendum 2) |
| Checkpoint and resume on reassignment, **on real peers** | Through the orchestrator with a lease-scoped token, so peers hold no storage keys (ADR-006 addendum 3). Before that, no installed peer ever checkpointed and every recovery restarted from step 0 -- both earlier `failure_recovery` artifacts show it |
| Adaptive placement beats the baselines on reliability | 6/6 vs 2/6 and 3/6, `bench/report/20260728T155702` |
| A node that lets an offer lapse is skipped briefly, not re-offered | `tests/test_claim_backoff.py` (M7.1c) |
| Multi-rank DDP under torchrun with real c10d rendezvous | ADR-005, M5 |
| Real user auth; no secret in the browser bundle | ADR-012; bundle greps clean for the admin key |
| Container isolation | `cap_drop=ALL`, read-only rootfs, no host network (ADR-007) |
| A peer with only Python (no Docker/toolkit/WSL2) can contribute | Trained to 96.67% on a node with Docker unreachable (ADR-007 addendum) |
| **Google sign-in**, optional, never creating an account | ADR-012 addendum; verified against a real Google client — an account's `google_sub` bound on a genuine sign-in |
| **Custom image datasets** uploaded, validated, trained on | ADR-014; a 3-class archive uploaded to real MinIO, fetched by the trainer over a presigned URL, digest verified, trained (see caveat below) |
| **Adding people from the dashboard** instead of SSH | ADR-012 addendum 2; `tests/test_users_api.py` |
| **GPU utilization above the report's 80% target**, for batch 256 | CIFAR-10 batch 256: 93.8% mean over the trainer's own training phase (p10 84%), 79.7% over the whole run including container start. `bench/report/20261002T175843-gpu_utilization.json`. Before the GPU-resident data path the same job averaged 44% |
| **Uploaded datasets above the 80% target** | A real 6-class, 17k-image upload (Intel scenes): 93.4% mean at batch 64 and 91.7% at batch 256 over the training phase, from 86.2% and 85.1% before decode-once. `bench/report/20261002T182653-gpu_utilization_custom.json` (baseline `20261002T181554`). The batch pipeline now delivers 68k images/s from GPU memory and 111k/s streamed from disk, against 2,695/s this GPU trains -- measured in the trainer image, not by the harness |
| Recovery still resumes with background checkpoint uploads | Detected 2.92 s, restored step 2814 at 8.39 s, job finished 22 s after the kill. `bench/report/20261002T180001-failure_recovery.json` |
| **Adaptive placement prefers the closer node** (the `γ·D_i` term) | Two identical containerised agents, one with 100 ms of real `tc netem` egress delay (RTT EWMA 26 vs 124 ms): adaptive 10/10 on the near node, least_loaded 8/10, round_robin 5/10. The audit row shows L and R equal and D 0 vs 1. `bench/report/20261002T192259-latency_placement.json` |
| **Control-plane overhead stays flat from 1 to 16 nodes** | Real agents at 1/2/4/8/16: scheduling a job 12.8-15.5 ms after submit at every size (report target < 500 ms), scheduler pass 5-7 ms, detector pass about 8 ms, orchestrator memory 80-84 MB; its CPU grows with heartbeat rate (4% at 1 node, 24% at 16 on this laptop). No healthy node declared dead at any size. `bench/report/20261002T191552-scalability.json` |
| **Peers update themselves** | The agent restarts into a newer bundle while idle and pulls a newer trainer image (ADR-015). A real agent at an old version exited for update within one check interval; the full installer loop on a real peer is not yet verified |
| **Downloading the trained model** | ADR-006 addendum 2; a July run's checkpoint streamed byte-identical to storage (sha256 `471b73f9…` on both sides), opening as a valid torch archive |

554 tests, all against a real Postgres. No mocked database, no simulated
failures outside `tests/`.

---

## What is NOT claimed

**No throughput speedup from distribution.** Everything was developed on one
laptop with one RTX 3050, where extra ranks contend for the same device. M5
measured `world_size=2` at 251 s against `world_size=1`'s 171 s — distribution
is a *cost* here. The architecture is built for many machines; the evidence for
that benefit does not exist yet and must not be implied.

**80% GPU utilization is not met at small batch sizes.** At batch 64 the
training phase averages 70% (MNIST) and 59% (CIFAR-10), same artifact. SmallCNN
does so little work per step that a batch of 64 cannot fill the GPU between
kernel launches; that is the model's size, not the data path, which is no
longer the bottleneck. All of it is one RTX 3050 laptop GPU.

**Large uploads are designed for, not yet demonstrated.** Uploaded datasets are
decoded once and streamed from disk when they exceed 40% of free GPU memory,
which is what lets one larger than RAM train at all. That streamed path is
exercised by tests and a direct throughput probe, but no job on a dataset
genuinely too big for the GPU has run end to end; the largest real upload
here is 17k images (200 MB decoded), which fits in GPU memory.

**The custom-dataset run proves the pipeline, not accuracy.** The end-to-end
verification trained to 100% on three classes of solid colour blocks — data that
is trivially separable. It demonstrates that upload, digest verification,
extraction, and ImageFolder loading work; it says nothing about how this system
performs on a real image set, and the number must never be quoted as a result.

**The `α` (load) term of the scheduler is untested.** Agents on one host read
the same `psutil.cpu_percent()` and the same NVML device, so their load cannot
differ. Every benchmark artifact says so in a machine-written `limitations`
block (ADR-013). The reliability term `β·R_i` and the latency term `γ·D_i` have
both been validated; the latency test used one fixed `tc netem` delay with no
jitter or loss, not a real long-distance link.

**The significant-spread constants are unvalidated assumptions.** `25.0` load
points and `50.0 ms` are argued from the domain (ADR-009 addendum), not
measured. A fleet of genuinely different machines is what would test them.

**The retry bound is a judgement, not a measurement.** `MAX_JOB_FAILURE_RETRIES=2`
caps how far a broken job spec can walk the fleet, but no data says 2 is the
right number — it is a small bound chosen to make the failure mode cheap, and
it is configurable for that reason.

**Scalability is measured to 16 nodes on one host, and only for the control
plane.** The `scalability` scenario grew a real fleet to 16 agents; every agent
shared one laptop's CPU and GPU with the orchestrator, so training throughput
and gradient-sync delay as nodes are added remain unmeasured, and nothing has
run past 16 nodes. Its first run found real bugs instead of numbers: cancelling
a job silenced its agent for 11 s while the trainer refused SIGTERM, and the
detector declared healthy nodes dead 48 times (`20261002T185648`, kept).

**Requests with a body cost about 45 ms extra under Docker Desktop on
Windows.** A heartbeat takes about 5 ms of work inside the container but 58 ms
measured at the server: Docker Desktop's port forwarding relays a client's
header and body writes as two segments and holds the second for a delayed ACK.
Bodyless requests (a claim: 10 ms) do not pay it. The figures above include it,
because that is how this deployment runs.

**No load testing beyond that.** Nothing here has been run with more than 16
nodes or with concurrent jobs at scale. Scheduler pass cost is O(candidates) per job and the audit trail
writes a row per candidate per decision; neither has been profiled. Custom
datasets add a second unprofiled path: every peer claiming a job downloads the
archive, and nothing has measured what a large dataset across many peers costs.

---

## Known gaps and where to start

### 1. The usability test has never been run with a real person

`docs/USABILITY-TEST.md` is a complete script for an unassisted run. It needs a
classmate who has not seen the system. **Do not simulate it** — an invented
finding is worse than an untested interface, because it looks like evidence.

This is now the highest-information item on the list. The three features added
since M11 were all built to remove friction that was inferred rather than
observed, and only a real session would say whether the right friction was
removed.

### 2. TLS: encrypted paths exist; the plain LAN port is still open

Peers can join encrypted two ways: Tailscale, which encrypts transport itself,
and the orchestrator's own Cloudflare tunnel from `demo.ps1 -Public`, which
terminates TLS with a certificate every machine already trusts. Uploaded
datasets and checkpoints travel the same encrypted path as everything else.

What remains: the orchestrator still listens on plain HTTP on the LAN
(port 8090), and a peer or browser pointed at that address sends tokens and
passwords unencrypted. The agent says so at start-up. There is no TLS of the
orchestrator's own (no certificate for a bare LAN IP that peers would trust
without manual setup), and the quick tunnel's address changes every time it
starts, so it suits a session rather than a permanent deployment; a named
Cloudflare tunnel or Tailscale is the long-lived answer.

### 3. Token revocation is bounded by TTL only

There is no revocation list for either node or user tokens. Disabling a user
account takes effect immediately (the row is re-read per request), but a stolen
token stays valid until it expires — 15 minutes by default. Accepted in
ADR-008/ADR-012; worth revisiting if this ever holds anything sensitive.

### 4. The rate limiter is per-process

In-process fixed-window counters, so N orchestrator replicas allow N times the
limit. ADR-010 deploys one. A second replica needs shared state (ADR-012 §7).

### 5. Removed nodes are hidden, not deleted

An admin removes a machine from the Machines page (or `DELETE /nodes/{id}`):
it leaves the list, its agent can no longer authenticate or mint a token, and
its row stays so the jobs it trained still name it. A node holding live work
is refused (409). There is no bulk "remove everything offline" yet, so a
fleet that accumulated many dead benchmark nodes is cleaned one at a time.

### 6. One model architecture, now enforced

`SmallCNN` is the only architecture. `MODEL` is an allowlist (`small_cnn`, or
its alias `cnn`) checked at submit; four historical jobs that claimed
`resnet18`, `resnet` or `m` (and trained SmallCNN) still read, because stored
specs are returned as-is. Supporting non-image data means new architectures
first, not a new dataset format.

### 7. The API client is current, and a test keeps it that way

`schema.gen.ts` was regenerated, and can now be regenerated without a running
orchestrator (`npm run generate:api:offline`). `tests/test_api_client_current.py`
fails whenever the API serves an operation the client lacks; against the old
client it named exactly the five routes added in this round. A few dashboard
calls still use `fetch` directly on purpose: login and sign-in options run
before any token exists, and chunked uploads send raw binary pieces with
per-piece retry.

### 8. Google sign-in bounds replay rather than preventing it

A Google ID token is a bearer credential for its whole ~1 h validity, and nothing
in it makes it single-use. Freshness is required (`iat` within 5 minutes), which
shrinks the window by an order of magnitude but does not close it. The real fix
is a server-issued single-use nonce; `models/nonce.py` already has the machinery
from the node auth path. Recorded in ADR-012 addendum §6.

### 9. No audit trail for account changes

Creating, disabling, and role changes are written to the application log with the
acting admin's username, but there is nothing queryable the way `job_events`
records job history. If accounts ever become contentious, that is the gap.

---

## Things that bit during development

Recorded because they cost real time and will cost it again.

**`docker exec … alembic upgrade head` runs the migrations baked into the
image.** If the image predates your migration, alembic reports success while
doing nothing, and the failure surfaces later as a confusing enum error. Run
alembic from the host against the published port. Full detail in
`docs/OPERATIONS.md`.

**Reliability counters were silently corrupted for weeks.** Before revision
`0007`, an offer nobody claimed was recorded as `EXPIRED` — the same state as a
node that took work and stalled — and counted as a failure. One node
accumulated seven fabricated failures, corrupting the exact input the adaptive
scheduler ranks on. Fixed by giving unclaimed offers their own `UNCLAIMED`
state, plus `scripts/repair_reliability_counts.py` for the historical rows.

**Unit tests with well-separated fixtures hid a real scoring bug for six
milestones.** The normalization defect (ADR-009 addendum) was invisible to
every test because the fixtures used utilisation 10/50/90 and RTT 20/60/100,
where the buggy and correct formulas agree. It took a benchmark with
realistically *similar* nodes to expose it. When testing a ranking function,
include candidates that are nearly identical — that is where ranking is hard.

**A setting absent from `deploy/compose.yaml` never reaches the container.** The
orchestrator service enumerates every environment variable it passes, so
`GOOGLE_OAUTH_CLIENT_ID` in `deploy/.env` did nothing at all: the variable
stopped at the compose boundary, the feature reported itself disabled, and every
test still passed because nothing in the suite crosses that boundary. Add new
settings to `compose.yaml` at the same time as `config.py`.

**Compose's `${VAR:-}` makes a variable present and empty, not absent.** An
empty string is not `None`, so the obvious fix for the above introduced a second
bug where an unconfigured deployment advertised the feature as on. Settings that
mean "unset" need a validator that treats blank as absent.

**A presigned URL is signed for one specific host.** The orchestrator signs
against `S3_ENDPOINT_URL`, which in compose is `http://minio:9000` — correct for
a peer container, unresolvable from a browser, and impossible to rewrite
client-side without breaking the signature. Anything a *browser* fetches from
object storage has to be streamed through the API instead (ADR-006 addendum 2).

**A new table must be added to `tests/helpers.ALL_TABLES`.** A table missing
from that tuple is not truncated between tests, so its rows leak forward and the
next test sees state it never created — surfacing as a unique-constraint
conflict on what should be a first insert.

---

## Repository conventions worth knowing

- **Nothing is fabricated.** No invented telemetry, no assumed reliability, no
  `time.sleep` standing in for work. `scripts/check_no_fake_data.sh` enforces
  what it mechanically can. See `CONTRIBUTING.md` for the full rules.
- **Every ADR that turned out to be incomplete has an addendum** rather than a
  quiet edit, so the reasoning trail stays honest. All thirteen:
  `ADR-003-addendum`, `ADR-004-addendum`, `ADR-004-addendum-2`, `ADR-004-addendum-3`, `ADR-005-addendum`,
  `ADR-005-addendum-2`, `ADR-006-addendum`, `ADR-006-addendum-2`, `ADR-006-addendum-3`,
  `ADR-007-addendum`, `ADR-009-addendum`, `ADR-012-addendum`,
  `ADR-012-addendum-2`.

  `ADR-012-addendum` is the one that reverses a decision outright: ADR-012 ruled
  external IdPs out, and the addendum records why that reasoning stopped holding
  rather than editing the original to pretend it never said so. Its §5 was later
  rewritten to reverse *itself* — the uniform refusal message it originally
  specified turned out to be a rule borrowed from a context where it made sense
  and this one where it did not.
- **Benchmark artifacts are committed, including the failing one.** The first
  `reliability_placement` run disproved the scheduler's central claim. Deleting
  it would have made a tidier story and a dishonest record.
- **Test doubles live only in `tests/`** and are named `Fake*`/`Stub*`.
