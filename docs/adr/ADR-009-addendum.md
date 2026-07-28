# ADR-009 addendum: normalization must preserve magnitude

## Status
Accepted. Amends ADR-009 (Scheduling).

## What the benchmark found

The M9 evaluation harness was built to test ADR-009's central claim — that
`S_i = α·L_i − β·R_i + γ·D_i` places jobs better than the baselines. Its first
real run said no.

Two nodes, identical hardware, deliberately divergent history built from real
outcomes (`docker kill` on real trainer containers for failures, real completed
MNIST runs for successes):

| node | recorded history | Wilson `R_i` |
| --- | --- | --- |
| healthy (node-17) | 2 successes, 0 failures | 0.3006 |
| degraded (node-16) | 0 successes, 3 failures | 0.0362 |

Placement over 6 trials each:

| scheduler | placed on healthy |
| --- | --- |
| adaptive | **3 / 6** |
| least_loaded | 0 / 6 |
| round_robin | 3 / 6 |

`adaptive` was indistinguishable from `round_robin`. It was not routing around
a node that had really failed three times.

Artifact: `bench/report/20260728T154205-reliability_placement.json`.

## Why

The recorded per-candidate breakdown made the cause unambiguous. From trials
4-6, where both nodes reported identical load:

```
healthy   l=0.0  r=0.3006  d=1.0  S= 0.1994   raw_rtt=60.27ms
degraded  l=0.0  r=0.0362  d=0.0  S=-0.0362   raw_rtt=52.78ms   <- selected
```

Lowest `S` wins, so the node with three failures won.

`_normalize_unknown_worst` was doing plain min-max: `(v - lo) / (hi - lo)`.
Dividing by the *observed spread alone* rescales whatever difference happens to
exist to the full 0..1 range, no matter how small it is. A 7.5 ms gap between
two agents on the same laptop's loopback — pure jitter — became `d` scores of
0.0 and 1.0, worth `γ × 1.0 = 0.5` of penalty. The genuine reliability
difference was worth `β × 0.264`. Noise outvoted evidence, roughly 2:1.

**This is not an artifact of running on one host.** The same defect fires on
real hardware: two peers on one campus network at 40 ms and 41 ms get `d`
scores of 0.0 and 1.0 — a full-scale latency penalty for 1 ms — while two peers
at 20 ms and 500 ms get *the same* 0.0 and 1.0. The score could not distinguish
"slightly different" from "wildly different" on either axis.

Min-max also had a discontinuity: all-equal values mapped to 0.0, but values a
microsecond apart mapped to 0.0 and 1.0. An infinitesimal change flipped a term
from inert to maximal.

## Decision

Normalize against **at least** a domain-meaningful spread:

```
score = (v - lo) / max(observed_spread, significant_spread)
```

- A difference smaller than what the domain considers meaningful now produces a
  proportionally small penalty instead of a full-scale one.
- A genuinely large spread still reaches 1.0, so real differences are not
  blunted.
- The discontinuity disappears: the function is continuous as the spread goes
  to zero.
- Unknown telemetry still maps to 1.0 (worst case). Rule 2 is unaffected —
  absent readings are still never treated as "fast" or "idle".

Two new settings, both audited with every decision like the existing weights:

| setting | default | reasoning |
| --- | --- | --- |
| `SCHEDULER_LOAD_SIGNIFICANT_SPREAD` | `25.0` | Load is 0..100. A quarter of the range: ordinary sampling jitter on an idle fleet stays small, while 80% against 20% still reaches ~1.0. |
| `SCHEDULER_LATENCY_SIGNIFICANT_SPREAD_MS` | `50.0` | Peers on a LAN or Tailscale overlay differ by single-digit ms of noise; a genuinely distant peer differs by tens. 50 ms sits above the noise floor and below a real WAN hop. |

These are declared constants with stated reasoning, not values tuned until the
benchmark looked good. They were chosen from the domain before the re-run, and
the re-run's result is reported as measured either way.

## Consequences

- Placement changes for any fleet whose nodes are close together on load or
  latency — which is most small fleets. Nodes that genuinely differ are ranked
  as before.
- The weights `α`, `β`, `γ` keep their meaning, but their *effective* balance
  changes: previously any term with any spread at all contributed its full
  weight, so the weights were close to decorative whenever the fleet was
  homogeneous. They now scale real differences.
- The significant-spread values are themselves untested assumptions. They are
  defensible, they are documented, and they are configurable — but no
  measurement here constrains them, and that is stated rather than implied
  away. A fleet with genuinely different hardware is what would test them.
- ADR-013's limitation stands unchanged: this run still could not differentiate
  the `L` and `D` terms, because the agents shared one machine. What it *did*
  test — and what caught this — is that those terms must not fabricate
  differences that are not there.

## Note on the process

The defect was in production code from M3 and survived every unit test, because
the tests used well-separated fixture values (utilisation 10/50/90, RTT
20/60/100) where min-max and the corrected formula agree. It took a real fleet
with realistically *similar* nodes to expose it — which is the argument for the
benchmark existing at all, and for reporting its first result honestly instead
of tuning until it flattered the design.
