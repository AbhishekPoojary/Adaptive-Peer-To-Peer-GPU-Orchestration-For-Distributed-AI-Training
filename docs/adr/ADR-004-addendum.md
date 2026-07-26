# ADR-004 addendum (M6): the concrete φ-accrual detector

## Status
Accepted (extends ADR-004)

## Context
ADR-004 fixed the *policy* — a φ-accrual-style adaptive threshold with a hard
5 s floor — but not the formula, the distribution, the window, or how a
declared failure feeds recovery. M6 implements the real, active detector, so
those choices are pinned here for review.

## The formula (as implemented in `orchestrator/services/failure_detection.py`)

Per node, at each detector tick:

1. **Interval window.** Take the last `PHI_ACCRUAL_WINDOW_SAMPLES` (default 20)
   heartbeat receive-times straight from `node_telemetry_samples.ts` — one row
   is written per heartbeat, so these are *real recorded* arrivals, not a
   separately maintained (and driftable) counter. The inter-arrival intervals
   are the successive differences.
2. **Fit Normal(μ, σ).** μ = mean interval, σ = population stddev of the window,
   floored at `PHI_ACCRUAL_MIN_STD_SECONDS` (default 0.5 s). The floor is the
   standard φ-accrual guard: it prevents divide-by-zero and stops a node with
   near-constant intervals from being declared failed on a single late tick
   (σ→0 would make φ explode for any t>μ).
3. **Suspicion.** With elapsed silence `t = now − last_heartbeat_at`:

   ```
   P_later(t) = 1 − Φ_norm((t − μ) / σ)        # Φ_norm = standard normal CDF (via erf)
   φ(t)       = −log10( max(P_later(t), 1e-12) )
   ```

   `φ = 3.0` means "under this node's *own* recent behaviour, a heartbeat this
   late has probability ≤ 10⁻³ (0.1%)". Declare **failed** iff
   `φ(t) ≥ PHI_ACCRUAL_THRESHOLD` (default 3.0) **and** `t ≥ HEARTBEAT_FLOOR_SECONDS`
   (5 s, the ADR-004 hard floor — applied *after* the φ test, so it can only
   ever *delay* a declaration, never accelerate it).
4. **Bootstrap.** A node with fewer than `PHI_ACCRUAL_MIN_INTERVALS` (default 3)
   intervals has no distribution to fit. Rather than fabricate one, it uses an
   explicit fallback: declare failed iff `t ≥ PHI_ACCRUAL_BOOTSTRAP_SILENCE_SECONDS`
   (default 10 s, still ≥ the 5 s floor). This is the same "no history → an
   honest conservative default, never a plausible-looking guess" stance as the
   reliability Beta prior (ADR-009).

### Why this satisfies the "adaptive, not fixed" requirement
At **6 s of silence**: a node whose intervals are ~2 s (μ≈2, σ floored ≈0.5)
gives `z=(6−2)/0.5=8`, `P_later≈6e-16`, `φ≈15` → **suspected**. A node whose
intervals are ~10 s (μ≈10) gives `z=(6−10)/σ<0`, `P_later>0.5`, `φ≈0` → **not
suspected**. Same 6 s gap, opposite verdicts, because the threshold is relative
to each node's observed cadence — exactly ADR-004's intent. This is the
property test in `tests/test_phi_accrual.py`.

### Honest note on the 5 s "target" vs. the 5 s floor
The project report lists a *detection < 5 s* target. ADR-004 also mandates a
*5 s hard floor* ("no node is ever declared failed faster than that"). These are
in direct tension: the floor makes sub-5 s detection impossible **by design**.
We honour the ADR (the safety floor) over the report's target and report the
real measured number, which is `5 s (floor) + up to one detector tick`. See the
M6 chaos-run log for the actual value.

## Wiring into recovery (the fast path)
A declared failure does **not** wait for the 30 s lease-TTL sweep. The detector,
in the same pass, transitions the node `ONLINE → OFFLINE`, and for every job the
node holds a current-epoch lease on, calls the **shared** reassignment routine
`orchestrator.services.leases.reassign_job_attempt`. That is the *same code
path* the TTL sweep uses — φ-accrual and TTL-expiry are two triggers into one
reassignment implementation, never duplicated logic:

* the failed node's lease(s) → `EXPIRED` with `lease_failure_count += 1` (a
  timeout/drop is a real reliability signal, ADR-009);
* cohort siblings → `RELEASED` (no fault, no reliability hit) via
  `_release_cohort_siblings`;
* the job → `REASSIGNED` under a cleared cohort/rendezvous wiring, so the next
  scheduler pass re-selects nodes at a fresh epoch. The `ONLINE`-only hard
  filter (ADR-009) means the just-failed node is excluded until it heartbeats
  again — no reassignment back onto the dead node.

The detector loop triggers an immediate scheduler pass after declaring any
failure, so reassignment→replacement isn't gated on the 3 s scheduler cadence —
this is what keeps total recovery inside the 15 s target.

## Timeline events (for the M7 dashboard, written now)
The reassignment JobEvent leads with plain language — `"node-01 stopped
responding (φ-accrual suspicion 12.4 ≥ 3.0 after 5.4 s of silence)"` — and the
subsequent SCHEDULED event names the new host. The trainer's resume emits a
`"Resumed from checkpoint at step N (epoch E)"` JobEvent (see ADR-006 addendum).

## Consequences
* No new orchestrator schema: the detector reuses `Node.status`,
  `Node.last_heartbeat_at`, and `node_telemetry_samples.ts`. Only ONLINE nodes
  are evaluated, and only an ONLINE→OFFLINE flip fires recovery, so an
  already-OFFLINE node is never re-processed and a benign single missed tick
  under the floor is never acted on.
* The detector runs as a real background asyncio loop
  (`FAILURE_DETECTOR_INTERVAL_SECONDS`, default 1 s), started alongside the M2
  scheduler/sweep loops and gated by the same `ENABLE_BACKGROUND_LOOPS` switch
  (off in tests, which drive `run_failure_detection_pass` deterministically).
