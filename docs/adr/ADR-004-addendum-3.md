# ADR-004 addendum 3: an acceptable heartbeat pause, and heartbeats on their own loop

## Status
Accepted (amends ADR-004 addendum 2)

## Context
Addendum 2 cut the silence floor to 3 s and the heartbeat to 1 s, and measured
detection at 2.4 s, on loopback. Its consequences section named the price, "less
tolerance for a stall", without anything to measure it against.

The first peer to join over a real internet path paid that price. A real agent
on a Cloudflare quick tunnel trained an uploaded dataset while it downloaded
that dataset through the same tunnel. Its heartbeats averaged 2.5 s apart
(std 0.7 s), because each request crossed the tunnel and back, and heartbeats
shared one loop with lease servicing. While the download saturated the tunnel,
one heartbeat arrived 4.9 s late. The Normal fit scored that φ 3.5, over the
3.0 threshold. A healthy peer was declared dead, its job was reassigned and
restarted, and it happened twice in one run. Each time also recorded a
reliability failure against a node that had not failed.

## Decision

### 1. An acceptable heartbeat pause (2 s)
φ is now computed against `mean + PHI_ACCRUAL_ACCEPTABLE_PAUSE_SECONDS`
instead of `mean`. This is the allowance Akka's φ-accrual detector calls
`acceptable-heartbeat-pause`, and it exists for exactly this. A Normal
distribution's tails are far too thin for network jitter: a 4.9 s gap is
3.5σ above that peer's mean, but on an internet link it is an ordinary event.

On loopback (mean about 1.3 s) the declaration moves from about 2.6 s of
silence to about 4.1 s, still inside the report's 5 s target, and is
re-measured below. For the tunnel peer above, the same 4.9 s stall no longer
crosses the threshold, while a peer that is really gone is declared about 2 s
later than before.

### 2. Heartbeats on their own task
The agent heartbeats from a dedicated task. A slow claim or renewal can no
longer push the next heartbeat back, so the interval the detector learns
reflects the network, not the agent's own workload. Token refresh moved with
it, and the lease loop reads whatever token is current. A guard restarts the
heartbeat if anything unexpected escapes it, because a node whose heartbeat
has silently died looks dead while it is still training.

## Consequences
* Detection on loopback is about 1.5 s slower than addendum 2 measured, and
  remains under 5 s (see STATUS.md for the re-measured figure).
* Detection now tolerates the jitter of the network peers actually use, which
  addendum 2's loopback measurement could not show.
* The allowance is configuration. A deployment whose peers sit on very poor
  links may want more; one entirely on a LAN could use less.
