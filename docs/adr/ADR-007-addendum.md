# ADR-007 addendum: opt-in unsandboxed execution on peers without Docker

## Status
Accepted. Amends ADR-007 (Isolation).

## Context

ADR-007 requires every training job to run in a container with `cap_drop=ALL`,
`no-new-privileges`, a read-only rootfs, tmpfs `/tmp`, memory and pid limits,
and no host networking. That is the right default and it is unchanged.

The cost is what a peer has to install before it can contribute a GPU:

- Docker,
- the NVIDIA Container Toolkit (for GPU passthrough into the container),
- on Windows, WSL2 — because GPU passthrough there needs the Windows driver
  *plus* the toolkit inside WSL.

That is three fiddly installs, the last of which defeats most people. For this
project's actual recruiting pitch — "lend me your laptop's GPU" to a friend who
is doing you a favour — it is enough friction to end the conversation. A fleet
of one perfectly-isolated node is worth less than a fleet of four.

The trainer's contract makes an alternative cheap. It is configured entirely by
environment variables and reports entirely on stdout (`METRIC`, `FINAL`,
`RESUME` lines). Nothing about it requires a container; the container is the
*isolation*, not the interface.

## Decision

**Docker stays the default. A peer without it may opt in to running the trainer
as a subprocess, and must say so explicitly.**

- Docker present → unchanged. Full ADR-007 isolation.
- Docker absent, no consent → the node still enrolls and heartbeats honestly,
  and fails any lease with "docker unavailable on this node". This is today's
  behaviour and remains the default.
- Docker absent, `--allow-unsandboxed` passed → the agent runs
  `python trainer/train.py` as a child process in its own virtualenv.

The flag is required, is off by default, and the agent prints exactly what is
being given up before it starts. Consent that is not informed is not consent,
and the machine at risk belongs to someone who is doing the operator a favour.

### What is actually lost

Stated plainly, because a vague warning is worse than none:

| ADR-007 control | Subprocess path |
| --- | --- |
| `cap_drop=ALL`, `no-new-privileges` | **Gone.** Runs as the peer's own user |
| Read-only rootfs | **Gone.** Can write anywhere that user can |
| tmpfs `/tmp`, pids limit | **Gone** |
| No host network | **Gone.** Has the user's network access |
| Memory limit (cgroups) | **Partially replaced** — see below |
| Filesystem isolation from the host | **Gone** |

### The memory guard is a mitigation, not a replacement

A container's memory limit is enforced by the kernel. A subprocess has no
equivalent that is portable to Windows, so the agent instead samples the child
process tree's RSS via `psutil` and kills it if it exceeds the configured
limit.

This is genuinely weaker: it is polled rather than enforced, so a fast
allocation spike can overshoot between samples, and a child that escapes the
process tree is not seen. It is offered because "no limit at all" was the
alternative, and a poll that catches a runaway training loop within a second is
worth having. It is not described anywhere as equivalent to cgroups.

### Why this is a defensible trade *here* and not in general

The peer runs one specific program — this project's `trainer/train.py` — whose
inputs are already constrained at the API boundary: `dataset` is a `Literal`
allowlist, `model` is a bounded string the trainer resolves against its own
implementations, and every numeric is range-checked (`JobSpec`, ADR-012 §4).
A submitter cannot smuggle arbitrary code through the job spec.

So the realistic risk is not "a malicious job escapes the sandbox" but "this
project's own trainer has a bug that damages the peer's machine" — a much
smaller surface, and one the peer is already accepting by running the agent at
all.

That reasoning does **not** generalise. If job specs ever carry a container
image, a script, a pip requirement, or a model definition supplied by the
submitter, the subprocess path must be removed, because at that point the
sandbox is the only thing standing between a submitter and every peer's
filesystem. This addendum is scoped to a trainer whose code the peer can read
before agreeing to run it.

## Consequences

- A peer with an NVIDIA driver (which they already have if they use the GPU)
  and Python needs **no further installs**. Windows works natively, no WSL2.
- The download shrinks: `pip install torch` is ~2.5 GB against the trainer
  image's 9.7 GB.
- Checkpointing, log streaming, metric reporting, lease renewal, fencing, and
  failure reporting are identical on both paths — they all sit above the
  launcher, so there is one execution contract with two backends rather than
  two subsystems.
- `bench/` artifacts must record which path a run used, since an unsandboxed
  run has different performance characteristics (no container overhead, no
  cgroup limits) and comparing across them silently would be dishonest.
- **The two paths run different PyTorch versions**, and that is deliberate. The
  container is pinned to `torch 2.5.1` by its image; the unsandboxed installer
  asks for `>=2.6,<3` because PyTorch publishes no Windows wheel for 2.5.1 on
  Python 3.13 — a peer on 3.13 could not install the pinned version at all
  (verified against the index; 2.6.0 is the first with a `cp313 win_amd64`
  build). The trainer uses standard APIs and runs unchanged on both, but a
  result produced on one path is not bit-identical to the other, and anything
  comparing them must say which was used.
- The agent bundle must now ship `trainer/` as well as `agent/`, because a peer
  running the subprocess path needs `train.py` locally.
