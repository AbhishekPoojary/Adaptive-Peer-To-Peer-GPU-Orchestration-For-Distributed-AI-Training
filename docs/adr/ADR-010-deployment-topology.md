# ADR-010: Deployment topology

## Status
Accepted

## Context
Development happens on a single Windows 11 laptop (RTX 3050, 4 GB VRAM) —
there is no multi-machine lab available yet, but the architecture must
not implicitly assume single-node deployment, since the real target is a
fleet of NATed peer laptops with mixed GPU/CPU hardware.

## Decision
Two topologies, explicitly distinguished in all reporting:
- **Dev**: docker compose, everything co-located on one Windows 11
  laptop. Multiple agent containers time-share the single physical GPU.
  Any benchmark/result produced this way is labeled "co-located
  multi-process" — never described as multi-host or WAN, because it
  isn't; it's one GPU serving several containers concurrently.
- **Multi-host** (once more machines are available): a Tailscale overlay
  network joins NATed peer laptops together. The orchestrator itself
  stays on the original laptop rather than requiring a public IP or
  cloud host, because agents always dial out (ADR-002) — the
  orchestrator only needs to be reachable on the overlay, not the public
  internet. CPU-only peers register with `gpu: null` (never a fabricated
  GPU record) and participate as honest CPU nodes using the gloo backend
  for `torch.distributed` (ADR-005).

## Consequences
- Every benchmark/bench report produced in dev must carry the
  "co-located multi-process" label (see `bench/schema.json`'s hardware
  block) so results are never mistaken for genuine multi-host network
  behavior — RTT/contention on one machine's GPU sharing is not the same
  as RTT across real peers.
- Moving to multi-host later is additive (join the Tailscale overlay,
  point agents at the orchestrator's overlay address) rather than a
  re-architecture, because ADR-002's dial-out design already assumes no
  public IP on either side.
- `gpu: null` nodes must be handled as a first-class case everywhere
  telemetry/scheduling touches GPU fields — there is no "assume every
  node has a GPU" shortcut anywhere in the codebase.
