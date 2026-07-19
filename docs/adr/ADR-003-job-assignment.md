# ADR-003: Job assignment

## Status
Accepted

## Context
Jobs must be handed to exactly one live agent at a time, survive an agent
crashing mid-job without a human intervening, and never end up
double-executed or silently lost because of a crashed/zombie agent that
reconnects late believing it still owns the work.

## Decision
Lease-based pull, not push:
- Agents pull available work; the orchestrator never pushes a job onto a
  specific agent's connection.
- A claim is a lease with a TTL; the agent must renew before expiry or the
  lease is reclaimable by another agent.
- Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED` so concurrent claim
  attempts from multiple agents don't block on each other or double-claim.
- Each lease carries a monotonically increasing `lease_epoch`. Any write
  from an agent (progress update, checkpoint, completion) must include the
  epoch it was leased under; writes from a stale epoch (a "zombie" agent
  that lost and reclaimed a lease, or reconnected after expiry) are
  rejected.

## Consequences
- No separate lock service needed — Postgres row locks are sufficient at
  this scale.
- The epoch fence means a slow/partitioned agent that comes back after
  losing its lease cannot corrupt state with a late write; it gets
  rejected and must re-claim.
- Requires every mutating agent-facing endpoint to check
  `lease_epoch` against the current stored value, which is extra
  discipline on every handler — worth it to avoid split-brain job state.
