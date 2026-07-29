# ADR-005 addendum 2: bounded retry on a reported trainer failure

## Status
Accepted. Amends ADR-005 and the terminal-failure behaviour in
`services/leases.fail_lease`.

## Context

ADR-005 made reported training failures **terminal**: if the agent watches the
trainer exit nonzero and says so, the job goes straight to `FAILED`. Only a
*dropped node* — heartbeat silence, lease expiry — was retryable.

The reasoning was sound and still is: a job whose spec is broken (bad model
name, a batch size nothing can fit, a code bug) fails identically everywhere.
Retrying it walks the same failure across the whole fleet, burning every peer's
GPU to learn what the first attempt already established. Failures should
surface, not be masked by endless retry.

What that reasoning missed is that **not every trainer failure is a property of
the job**. M9 made this concrete. The `failure_recovery` scenario originally
killed the trainer container with `docker kill`; the job went to `FAILED` at
lease epoch 1 with `trainer exited with code 137` and was never retried.

Exit code 137 is `128 + 9` — `SIGKILL`. On the target deployment for this
system, ADR-010's fleet of student laptops, the overwhelmingly likely source of
a `SIGKILL` is the OOM killer on a 4 GB consumer GPU. That is a property of
*that machine*, not of the job. A peer with 8 or 24 GB would very likely
succeed, and the current behaviour throws that possibility away on the first
attempt.

So the system is least fault-tolerant against the single most probable real
failure in the deployment it was designed for.

## Decision

**Retry a failed attempt on a bounded counter, and prefer a different peer.**

A job carries `failed_attempt_count`. On a reported trainer failure:

- if `failed_attempt_count < MAX_JOB_FAILURE_RETRIES`, increment it, put the
  failing node into a short scheduling backoff, and move the job to
  `REASSIGNED` — the same retry path a dropped node already takes;
- otherwise, `FAILED` terminally, with an audit message stating how many
  attempts were made and on which nodes.

### Why a bound rather than classification

The obvious alternative is to classify the failure — treat 137/139 as
node-specific and retry those, treat a clean exit 1 as job-specific and fail
fast. It was rejected because **the classification cannot be made reliably**.
Exit 137 means the process received `SIGKILL`; it does not say who sent it. The
OOM killer, a `docker kill` from an operator, a container runtime cleaning up
under memory pressure, and a peer's own shutdown script are indistinguishable
from the exit code alone. Building a heuristic that looks authoritative on top
of a signal that genuinely is not would be exactly the kind of plausible
fabrication this codebase forbids elsewhere.

A bound does not need to know why. With the default of 2 retries a broken job
spec costs 3 attempts across the fleet and then fails loudly — bounded waste,
which was the entire objection — while a node-specific failure gets real second
and third chances on different hardware. It is a worse fit than perfect
classification and a much better fit than perfect classification's absence.

### Why the failing node is backed off rather than banned

The retry reuses M7.1c's `scheduling_backoff_until`, so the next placement pass
skips the node that just failed if anything else is free. It is a short window,
not an exclusion: on a single-node fleet the job must still be able to retry
where it is, because "no other peer exists" should delay a retry, not cancel
it.

Reliability accounting is unchanged. Every failed attempt still increments the
node's `lease_failure_count`, because a trainer that died on that node *is* a
real recorded outcome for it — this is precisely the earned signal ADR-009
ranks on, and a node that keeps killing trainers will be deprioritised by the
adaptive scheduler on its own.

## Consequences

- A job can now consume up to `1 + MAX_JOB_FAILURE_RETRIES` attempts before
  failing. Wall-clock time to a *terminal* failure grows accordingly; the
  audit trail states the attempt count so a slow failure is never mysterious.
- `Job.failed_attempt_count` is distinct from `current_lease_epoch`, which also
  increments for dropped-node reassignments. Conflating them would let node
  churn exhaust a job's failure budget.
- A genuinely broken job spec now burns 3 peers instead of 1. That is the
  accepted cost of the trade, and the reason the bound is small and
  configurable rather than generous.
- Setting `MAX_JOB_FAILURE_RETRIES=0` restores the exact pre-M11 behaviour, so
  a deployment that prefers fail-fast can have it without a code change.
- The M9 `failure_recovery` scenario deliberately kills the *agent* rather than
  the container, because that was the retryable path when it was written. It
  remains valid and still measures node-loss recovery. The container-kill path
  it originally used is now covered by `tests/test_failed_attempt_retry.py`.
