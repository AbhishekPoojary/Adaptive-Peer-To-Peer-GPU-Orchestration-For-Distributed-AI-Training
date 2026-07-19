# ADR-007: Isolation

## Status
Accepted

## Context
Training jobs run arbitrary(-ish) code from the job submitter on a peer's
own machine. The peer's host system, other jobs, and other tenants on the
same GPU need protection from a misbehaving or malicious job, without
requiring peers to run a full hypervisor-based VM per job (too heavy for
consumer hardware time-sharing a single GPU).

## Decision
Docker-based isolation per job container:
- GPU access via Docker's `--gpus` flag (NVIDIA runtime), scoped to the
  specific device(s) assigned, not all GPUs on the host.
- Dropped Linux capabilities (no `--privileged`, minimal `--cap-add`).
- Read-only root filesystem, with explicit writable mounts only for
  dataset/checkpoint paths that need it.
- No host networking (`--network` is a job-scoped bridge/none, never
  `host`).
- cgroup limits on CPU/memory so one job can't starve the host OS or
  sibling containers.

## Consequences
- Meaningfully weaker isolation than a VM boundary — accepted for M0
  because the threat model is "buggy or resource-hungry job," not
  "actively hostile job from an untrusted stranger" (that threat model
  would require reconsidering this ADR).
- Read-only rootfs means any job that assumes it can write to arbitrary
  paths in its own image will fail fast and visibly, which is preferred
  to silently succeeding with hidden state that vanishes on container
  restart.
- Per-job GPU scoping is what makes multiple time-shared job containers
  on one physical GPU (ADR-010's dev topology) safe to run side by side.
