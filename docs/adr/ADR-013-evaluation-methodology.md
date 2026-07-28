# ADR-013: Evaluation methodology — what this hardware can honestly measure

## Status
Accepted

## Context

ADR-009 makes a falsifiable claim: placing jobs by the penalty score
`S_i = α·L_i − β·R_i + γ·D_i` produces better outcomes than the
`round_robin` and `least_loaded` baselines. M9 is supposed to test it.

The available hardware is one Windows laptop with one RTX 3050 (ADR-010).
Multiple agents can run on it — each a real process with a real identity, real
lease history, and a real heartbeat — but they are not independent machines,
and that has consequences that no amount of harness engineering removes:

- **`L_i` (load) is not differentiable.** `agent/telemetry/system.py` reads
  `psutil.cpu_percent()`, which is host-wide, and `agent/telemetry/nvml.py`
  reads `nvmlDeviceGetUtilizationRates`, which is device-wide. Two agents on
  this laptop observe the *same physical machine* and therefore report the
  same load, truthfully. Loading one "node" loads all of them.
- **`D_i` (latency) is not differentiable.** Every agent dials `localhost`.
  Measured RTT differs only by scheduling noise, not by network distance.
- **Aggregate throughput cannot improve.** All ranks contend for one GPU. M5
  already measured this directly: `world_size=2` took 251 s against
  `world_size=1`'s 171 s. Distribution is a *cost* on one machine, not a
  speedup.

Two dishonest ways out were available and are rejected explicitly:

1. **Synthesize per-node load.** Have each agent report a per-process or
   assigned utilization figure so nodes look different. This violates rule 2
   ("telemetry comes from hardware or is reported as null") and would make the
   headline result an artifact of the fake input.
2. **Report the speedup that would be expected on real hardware.** A modelled
   number presented next to measured ones is fabrication regardless of how it
   is labelled.

## Decision

**M9 measures the reliability term, and says so.**

`R_i` *is* differentiable on this hardware, and legitimately so: reliability is
per-node lease history, and each agent accumulates its own from real recorded
lease outcomes. A node whose trainers were really killed really does carry
recorded failures. Nothing is simulated to produce that difference — the
failures are induced with `docker kill` against real running containers, which
is what rule 6 already demands of failure testing.

That makes the honest experiment a sharp one rather than a weak one, because
reliability is exactly where `adaptive` and `least_loaded` must disagree:

> Given two nodes that are equally loaded and equally close — which, on this
> hardware, they unavoidably are — `least_loaded` cannot tell them apart, and
> `round_robin` will not try. Only `adaptive` has an input that distinguishes
> them: the recorded lease history. So an **idle but unreliable** node is the
> discriminating case, and placement share is the measurement.

This is also the term the project most depends on being right: it is the input
M7.1a's bug was corrupting with fabricated `UNCLAIMED` failures, and the reason
that fix blocked M9.

### Scenarios

1. **`reliability_placement`** — the headline. Build real divergent history on
   otherwise-identical nodes (real trainer kills on one, real completions on
   the other), then submit jobs under each of the three schedulers and record
   where each landed. Reports placement share per node per scheduler, plus the
   orchestrator's own recorded `S_i` breakdown for every adaptive decision.
2. **`failure_recovery`** — kill a running trainer's container mid-training and
   measure real wall-clock recovery: detection, reassignment, and whether the
   job still reaches a correct final accuracy.

### Every artifact declares its own limits

The harness writes a required `limitations` block into every report naming
the terms that were **not** exercised (`load_term_differentiable: false`,
`latency_term_differentiable: false`) with the reason. This is machine-written
alongside the numbers, not a footnote in prose someone can quote around. A
reader who takes only the JSON still cannot mistake this for a validation of
the whole score function.

### The harness refuses to fabricate

A scenario that cannot complete a measurement writes **no artifact** and exits
non-zero. It does not emit a partial report with nulls that a later reader
might average over, and it does not fill a gap with a default. An absent
artifact is an honest "this did not run"; a plausible one is a lie.

## Consequences

- The project can claim, with evidence: *the adaptive scheduler demonstrably
  routes around nodes that have really failed, where the baselines do not.*
  It cannot claim a throughput speedup, and M10's write-up must not imply one.
- `α` and `γ` remain untested. They are not unused — they are in the shipped
  scoring path and every decision logs them — but no measurement here
  constrains their values. Stated rather than quietly left ambiguous.
- The scenarios are written against the real HTTP API with a real user token
  (ADR-012), so they exercise the same path a human does. When real peer
  laptops arrive, `reliability_placement` runs unchanged and
  `load_term_differentiable` becomes true without a harness rewrite — the
  limitations block is computed from the observed fleet, not hardcoded.
- Runtime is dominated by real training and real failure detection (~5 s floor
  per detection, ADR-004). These are minutes-long runs, not seconds. Accepted:
  the alternative is not measuring the real system.
