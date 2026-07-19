# ADR-004: Failure detection

## Status
Accepted

## Context
Peer nodes are consumer hardware on home networks: heartbeat gaps happen
for benign reasons (Wi-Fi hiccups, laptop sleep, CPU contention) as often
as for real failures. A fixed short timeout produces false-positive
failure declarations; a fixed long timeout delays real failure detection.

## Decision
Missed-heartbeat detection using a φ-accrual-style adaptive threshold: the
detector maintains a running distribution of recent heartbeat intervals
per node and computes a suspicion level (φ) from how anomalous the current
gap is relative to that node's own recent history, rather than a single
global timeout. A hard floor of 5 seconds applies regardless of history —
no node is ever declared failed faster than that.

## Consequences
- Nodes with naturally jittery connections get a more tolerant effective
  threshold instead of being flapped in and out of the fleet.
- Requires storing/maintaining a per-node heartbeat interval history
  (feeds directly into the reliability signal used by ADR-009's scoring).
- More complex than a fixed timeout; justified because the fleet is
  heterogeneous consumer hardware, not a homogeneous datacenter.
- The 5 s floor is a deliberate, hand-set safety bound, not derived from
  data — it exists to prevent the adaptive threshold from ever firing
  faster than is physically reasonable to detect over commodity networks.
