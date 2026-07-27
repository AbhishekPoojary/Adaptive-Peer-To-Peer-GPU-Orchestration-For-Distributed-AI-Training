# ADR-003 addendum (M8): what a lapsed lease actually proves

## Status
Accepted (extends ADR-003; refines the reliability input defined in ADR-009)

## Context

ADR-003 fixed the lease protocol (pull-based claim, TTL, epoch fence) and
ADR-009 fixed reliability as "a Wilson lower bound over that node's *recorded
lease history* (successes vs. failures/timeouts)". CONTRIBUTING.md rule 4 makes
that binding: reliability must be **earned** — derived from real recorded
outcomes, never assigned.

The M5 cohort model introduced a two-phase grant: the scheduler creates N
`PENDING` slots at a minted epoch, and each rank's agent later *claims* its slot,
flipping it `ACTIVE`. The TTL sweep then had two different things to sweep, and
treated them identically — both became `EXPIRED` with `lease_failure_count += 1`.

A real incident showed why that is wrong. Job `d2fc2a9f` (8-epoch MNIST) burned
**7 lease epochs in 108 s** before training started, because the node it was
being offered to was still busy running an orphaned container from a cancelled
job and never polled to claim. Every one of those 7 lapsed offers was recorded
as that node's failure. `node-14` ended with `lease_success_count=2,
lease_failure_count=8` — **7 of the 8 failures were for work it was never
given**, on a node that completed every job it actually ran. That is exactly the
fabricated-reliability failure mode rule 4 exists to prevent, and it feeds
straight into the M9 adaptive-vs-baseline benchmark, whose whole subject is the
reliability term.

## Decision

**A lapsed lease is attributed by the state it was in when it lapsed.**

| Event | Terminal state | Reliability |
|---|---|---|
| `ACTIVE` lease passes TTL without renewal | `EXPIRED` | `lease_failure_count += 1` — a real failure |
| `PENDING` slot passes TTL without ever being claimed | `UNCLAIMED` | **no effect** |
| Agent reports a failure | `FAILED` | `lease_failure_count += 1` |
| Lease finished | `COMPLETED` | `lease_success_count += 1` |
| Cancelled / cohort sibling torn down | `RELEASED` | no effect |

The reasoning for the new row:

* An **`ACTIVE`** lease past TTL means the node *took the work on* and then
  stopped making progress. Whatever the cause, the work it accepted did not get
  done. That is a genuine, earned reliability signal, and it is unchanged.
* A **`PENDING`** slot past TTL means the *scheduler offered* work that was
  never picked up. That may be the node's fault (a dead agent) or the system's
  (an agent still busy with earlier work, an orphaned container, a claim-poll /
  TTL race, a node that just went offline). **The event itself does not say
  which**, so it earns no penalty — the same instinct the sweep already applied
  to cohort siblings, which are `RELEASED` because "the drop was not their
  fault".
* Nothing is lost by not penalising it, because a genuinely dead node is already
  caught by a *different* real signal: the M6 φ-accrual detector (ADR-004)
  declares it `OFFLINE` from its own recorded heartbeat history and penalises it
  through `reassign_job_attempt`. The PENDING-expiry penalty was therefore both
  wrong and largely redundant.

`UNCLAIMED` is a new `lease_state` enum value (migration
`0007_unclaimed_lease_state`), not a flag, because reliability is computed by
reading `leases` rows directly
(`services.scheduling._reliability_inputs` weights `FAILED`/`EXPIRED` rows by
decayed age). Leaving these rows as `EXPIRED` and only skipping the flat
counter would have left the actual `R_i` input poisoned while looking fixed.

### Deliberate asymmetry: the φ-accrual detector still penalises PENDING

`failure_detection._reassign_node_jobs` passes *every* current-epoch lease the
dead node holds — `PENDING` included — as `failed_leases`. This is intentional
and is not the case above: there, fault has already been established from an
independent recorded signal (the node stopped heartbeating). The TTL sweep has
no such evidence; the detector does.

### Timeline wording

The job timeline must let a reader tell the two apart, so the sweep writes
different sentences:

* `"1 cohort lease(s) expired without progress; the whole attempt was torn down
  and requeued …"`
* `"node-14 did not pick up the work in time (the offer went unclaimed for its
  15s lease TTL); rescheduling. No reliability penalty: the node never took this
  work on."`

### Metrics

`orchestrator_lease_expiries_total` now counts only node-at-fault expiries. The
new `orchestrator_lease_offers_unclaimed_total` counts lapsed offers — a
*scheduling efficiency* series, deliberately not a reliability one.

## Consequences

* Reliability (`R_i`) is now only moved by evidence about the node: work it
  finished, work it reported failing, work it held and dropped, or a detected
  death. Offers it never answered do not move it.
* Existing rows recorded under the old semantics are still mislabelled. They are
  repaired by the one-off, auditable `scripts/repair_reliability_counts.py`
  (see its module docstring), which reclassifies `EXPIRED`-but-never-claimed
  leases using an independent authoritative record — the presence of a
  `job_events` grant event carrying that `lease_id`, written by
  `claim_job_for_node` — and then recomputes every node's counters from the
  `leases` table. No number is invented; every corrected value is derivable from
  recorded history.
* An agent that is wedged or busy no longer accumulates fake failures — but it
  can still absorb repeated offers, which is a *scheduling* bug in its own
  right. That is bounded separately by the unclaimed-offer backoff (below).

## Related change: bounded backoff after an unclaimed offer

Not penalising the thrash does not stop the thrash. In the incident the sweep's
`REASSIGNED` immediately triggered a scheduler pass which re-offered the same job
to the same busy node ~every 15 s, seven times.

`services.scheduling` now avoids re-offering a job to a node that just let that
job's slot lapse, for

```
delay = min(SCHEDULER_UNCLAIMED_BACKOFF_SECONDS * 2^(n-1),
            SCHEDULER_UNCLAIMED_BACKOFF_MAX_SECONDS)
```

measured from that slot's `expires_at`, where `n` is the number of `UNCLAIMED`
leases already recorded for that (job, node) pair — read from the `leases` table,
so the backoff state is real recorded history and needs no in-memory bookkeeping
that a restart would lose. Defaults: 30 s base, 300 s cap.

Scope is deliberately per **(job, node)**, not per node: a node that cannot take
*this* job right now may still be the right home for another one, and a
fleet-wide penalty on a single-node dev cluster (ADR-010) would stall everything.
The job is not starved — it stays `REASSIGNED` and is re-offered when the window
passes, or goes to any other eligible node immediately.
