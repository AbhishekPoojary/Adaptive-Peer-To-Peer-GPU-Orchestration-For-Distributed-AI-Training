# ADR-004 addendum 2: a 3 s floor, a 1 s heartbeat, and RUNNING at launch

## Status
Accepted (amends ADR-004 and ADR-004 addendum)

## Context
The project report sets two fault-tolerance targets: a dead peer **detected
within 5 s**, and its work **recovered within 15 s**. The first measured run
(`bench/report/20260728T161648-failure_recovery.json`) missed both:

* **Detection, about 5.8 s.** The ADR-004 addendum already said why, and called
  the gap deliberate: ADR-004 fixed a 5 s hard floor, which makes "under 5 s"
  impossible by construction, and the addendum chose the floor over the target.
* **Recovery, about 21 s** from the kill to the replacement being recorded as
  running.

On a closer look, the second number was partly a measurement artifact rather
than real time. The orchestrator records `LEASED -> RUNNING` on a lease's
*first renewal*, and the agent renewed on a timer: once fewer than
`--lease-renew-margin-seconds` (5) remained on a 15 s lease. So "training
started" was stamped about 10 s after the claim, whether or not training had
already started. That run's `LEASED -> RUNNING` gaps were 11.7 s and 11.6 s:
the timer, not the trainer.

## Decision

### 1. Floor 5 s -> 3 s, agent heartbeat 2 s -> 1 s, detector tick 1 s -> 0.5 s
ADR-004 called the 5 s floor "a deliberate, hand-set safety bound, not derived
from data". It was sized against a 2 s heartbeat: two and a half missed beats.
With a 1 s heartbeat, a 3 s floor means three missed beats, a *stricter*
margin in beats than before while being 2 s faster in wall-clock terms.

The adaptive part is unchanged and still does the work ADR-004 wanted it for.
φ is computed against each node's own interval distribution, so a node with a
jittery link has a wider fitted σ and crosses the threshold later; the floor
only limits how early a *steady* node can be declared dead. For a node beating
every ~1.1 s with σ at its 0.5 s floor, φ reaches 3.0 at about 2.6 s of
silence, so the 3 s floor is still what binds.

The tick drops to 0.5 s so it adds at most half a second on top of the floor.

### 2. The agent reports RUNNING when the trainer launches
The execution task sets an `asyncio.Event` once the container (or unsandboxed
process) is running. The agent's next cycle (now at most 1 s later) sends the
first renewal immediately instead of waiting for the timer, which is the
renewal that moves the job to `RUNNING`. It fires once; later renewals go back
to the timer. A failed send is not marked as sent, so it is retried on the next
cycle rather than falling back to the timer's lag.

No orchestrator change and no new endpoint. The renew endpoint already
documents its first call as the `RUNNING` transition (`services/leases.py`);
this only makes the agent send it at the right moment.

### 3. The benchmark reports stages, not one number
`failure_recovery` now records, each measured **from the kill** (not from the
last heartbeat, which would flatter detection by however long the peer had
already been silent):

* `detection_seconds`: the job goes `REASSIGNED`
* `relet_seconds`: a surviving peer holds the new lease
* `training_restarted_seconds`: the replacement trainer is running
* `checkpoint_resume_seconds`: the replacement has loaded the dead peer's
  checkpoint, when it did (with `resumed_from_checkpoint` stating whether)

Event times come from the orchestrator's clock and the kill time from the
host's, so the artifact measures the offset between them at submit time and
records both the correction and its error bound.

## Consequences
* **Twice the heartbeat traffic.** One telemetry row per heartbeat, so twice
  as many writes per node. That is fine for the handful of nodes this has run
  with; ADR-013 already records that nothing has been load-tested.
* **Less tolerance for a stall.** A peer whose agent freezes for 3 s, through
  a GC pause, a suspended laptop or a saturated uplink, is now declared dead
  where it would previously have survived up to 5 s. The cost of a false
  positive is bounded: the job is reassigned and resumes from its checkpoint,
  and the node rejoins on its next heartbeat. It also costs one entry in the
  node's reliability record, which is the real price and the reason the floor
  was not pushed lower still.
* **Idle peers notice offered work sooner.** Claims ride the heartbeat loop,
  so a 1 s heartbeat also halves the worst-case wait between a job being
  offered and a peer claiming it.
* Both values remain configuration (`HEARTBEAT_FLOOR_SECONDS`,
  `FAILURE_DETECTOR_INTERVAL_SECONDS`, `--heartbeat-interval-seconds`). A
  deployment over slower links should raise them, and should raise the agent
  heartbeat and the floor together so the floor stays several beats long.
