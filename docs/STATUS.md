# Project status

An honest account of what this system does, what has been measured, and what is
missing. Written for whoever picks it up next — including a future me who has
forgotten the details.

Last updated: 2026-07-28, at commit `9179535`.

---

## What works

| Capability | Evidence |
| --- | --- |
| A peer enrolls with one command and appears in the dashboard | `docs/screenshots/`, `installer/` |
| Real training on real data, real backprop, real held-out accuracy | 99.03% MNIST test accuracy, `bench/report/20260728T161648` |
| Lease-based pull assignment survives NAT (agents dial out only) | ADR-003; agents ran with no inbound ports open |
| Epoch fencing rejects a zombie leaseholder's writes | `tests/test_lease_fencing.py` |
| Claim races resolve to exactly one winner | `tests/test_lease_claim_race.py` (real Postgres row locks) |
| φ-accrual detects a vanished peer | 5.8 s of silence → suspicion 11.9 vs threshold 3.0 |
| A job survives its node disappearing | Reassigned and completed, 55.89 s end to end |
| Checkpoint to MinIO and resume on reassignment | ADR-006, `tests/test_checkpoint_manifest.py` |
| Adaptive placement beats the baselines on reliability | 6/6 vs 2/6 and 3/6, `bench/report/20260728T155702` |
| A node that lets an offer lapse is skipped briefly, not re-offered | `tests/test_claim_backoff.py` (M7.1c) |
| Multi-rank DDP under torchrun with real c10d rendezvous | ADR-005, M5 |
| Real user auth; no secret in the browser bundle | ADR-012; bundle greps clean for the admin key |
| Container isolation | `cap_drop=ALL`, read-only rootfs, no host network (ADR-007) |

279 tests, all against a real Postgres. No mocked database, no simulated
failures outside `tests/`.

---

## What is NOT claimed

**No throughput speedup from distribution.** Everything was developed on one
laptop with one RTX 3050, where extra ranks contend for the same device. M5
measured `world_size=2` at 251 s against `world_size=1`'s 171 s — distribution
is a *cost* here. The architecture is built for many machines; the evidence for
that benefit does not exist yet and must not be implied.

**The `α` (load) and `γ` (latency) terms of the scheduler are untested.** Two
agents on one host read the same `psutil.cpu_percent()` and the same NVML
device, and both dial loopback, so those terms could not be differentiated.
Every benchmark artifact says so in a machine-written `limitations` block
(ADR-013). Only the reliability term `β·R_i` has been validated.

**The significant-spread constants are unvalidated assumptions.** `25.0` load
points and `50.0 ms` are argued from the domain (ADR-009 addendum), not
measured. A fleet of genuinely different machines is what would test them.

**No load testing.** Nothing here has been run with more than a handful of
nodes or jobs. Scheduler pass cost is O(candidates) per job and the audit trail
writes a row per candidate per decision; neither has been profiled.

---

## Known gaps and where to start

### 1. A reported trainer failure is terminal, even when it is node-specific

`services/leases.py` treats any nonzero trainer exit as terminal, on the
reasoning that a broken job spec would otherwise loop across the whole fleet.
That is sound for a bad spec and wrong for exit code 137 — `SIGKILL`, which on
a 4 GB laptop GPU is very often the OOM killer. A larger peer might well
succeed, and the job dies instead.

Fixing it properly means classifying failures (node-specific vs job-specific)
and bounded retry with an attempt counter, so a genuinely broken job still
fails loudly after N tries. This is the most valuable outstanding change: OOM
on small consumer GPUs is the single most likely real failure in the target
deployment.

### 2. The usability test has never been run with a real person

`docs/USABILITY-TEST.md` is a complete script for an unassisted run. It needs a
classmate who has not seen the system. **Do not simulate it** — an invented
finding is worse than an untested interface, because it looks like evidence.

### 3. No TLS

ADR-010 assumes a Tailscale overlay, which encrypts transport. Exposing the
orchestrator on any other network would put bearer tokens on the wire in
plaintext. Anything beyond the overlay needs TLS terminated in front.

### 4. Token revocation is bounded by TTL only

There is no revocation list for either node or user tokens. Disabling a user
account takes effect immediately (the row is re-read per request), but a stolen
token stays valid until it expires — 15 minutes by default. Accepted in
ADR-008/ADR-012; worth revisiting if this ever holds anything sensitive.

### 5. The rate limiter is per-process

In-process fixed-window counters, so N orchestrator replicas allow N times the
limit. ADR-010 deploys one. A second replica needs shared state (ADR-012 §7).

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

---

## Repository conventions worth knowing

- **Nothing is fabricated.** No invented telemetry, no assumed reliability, no
  `time.sleep` standing in for work. `scripts/check_no_fake_data.sh` enforces
  what it mechanically can. See `CONTRIBUTING.md` for the full rules.
- **Every ADR that turned out to be incomplete has an addendum** rather than a
  quiet edit, so the reasoning trail stays honest. See `ADR-003-addendum`,
  `ADR-004-addendum`, `ADR-005-addendum`, `ADR-006-addendum`,
  `ADR-009-addendum`.
- **Benchmark artifacts are committed, including the failing one.** The first
  `reliability_placement` run disproved the scheduler's central claim. Deleting
  it would have made a tidier story and a dishonest record.
- **Test doubles live only in `tests/`** and are named `Fake*`/`Stub*`.
