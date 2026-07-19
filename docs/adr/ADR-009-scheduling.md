# ADR-009: Scheduling

## Status
Accepted

## Context
Job placement must account for how busy a node already is, how reliable
it's actually been (not assumed), and how responsive it is on the
network — and must be swappable/comparable against simpler baselines
without rewriting the assignment path.

## Decision
A penalty score `S_i = α·L_i − β·R_i + γ·D_i` per candidate node `i`,
where:
- `L_i` — load, an EWMA of the node's recent utilization.
- `R_i` — reliability, a Wilson-lower-bound estimate over that node's
  recorded lease history (successes vs. failures/timeouts) with a time
  decay so old history matters less than recent behavior. Reliability is
  never assigned or defaulted to an optimistic constant for a new node —
  it is computed from whatever history exists (which may be little/none).
- `D_i` — a normalized EWMA of measured round-trip latency to the node.

Hard filters (GPU memory fits, required capability present, node not
already at capacity, etc.) run before scoring — scoring only ranks nodes
that already pass every hard requirement. Lower `S_i` wins.
`RoundRobin` and `LeastLoaded` are implemented behind the same scheduler
interface as this scorer so they can run as A/B baselines. Every
assignment decision (candidates considered, scores, filters applied,
winner) is audit-logged.

## Consequences
- New nodes with no lease history get a low/neutral reliability estimate
  from the Wilson bound's behavior at zero samples, not a fabricated
  "assume reliable" default — they may be scored conservatively until
  they build history.
- α/β/γ are tunable (see `deploy/.env.example`); changing them changes
  placement behavior without a code change, but also means scheduling
  quality is sensitive to values nobody has tuned yet in M0.
- The shared interface means swapping strategies (or running an A/B) is a
  config change, not a redeploy of different code paths.
- Audit logging every decision adds write volume proportional to
  scheduling frequency; accepted because "why did this job land here" is
  a required debuggability property, not a nice-to-have.
