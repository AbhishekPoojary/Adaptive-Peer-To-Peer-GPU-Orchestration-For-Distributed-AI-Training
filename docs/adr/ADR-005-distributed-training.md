# ADR-005: Distributed training

## Status
Accepted

## Context
Training must span multiple heterogeneous peer nodes (mixed GPU/CPU,
different reliability), tolerate a node dropping mid-job, and avoid
depending on infrastructure peer machines can't reasonably run
(etcd cluster, dedicated rendezvous service).

## Decision
Use `torch.distributed` DDP launched via `torchrun`, with c10d rendezvous
(no etcd dependency) and elastic execution (`--max-restarts`) so a worker
crash triggers a bounded number of automatic re-formations of the process
group instead of killing the whole job. The rendezvous host for a given
training cohort is the node the orchestrator currently scores as
highest-reliability among that cohort's members (ADR-009's `R` term),
re-evaluated when cohort membership changes.

## Consequences
- No etcd or third-party rendezvous backend to install/operate on peer
  machines — c10d rendezvous is built into PyTorch.
- Rendezvous host selection is dynamic, not fixed at job submission,
  which means the orchestrator must be able to hand off rendezvous-host
  duty if the current host's reliability score drops or it fails
  outright.
- `--max-restarts` bounds retry storms but means a cohort that keeps
  losing workers eventually fails the job rather than retrying forever —
  intentional, so failures surface instead of masking a systemically bad
  cohort.
- CPU-only members of a cohort participate via the gloo backend
  (ADR-010), not NCCL.
