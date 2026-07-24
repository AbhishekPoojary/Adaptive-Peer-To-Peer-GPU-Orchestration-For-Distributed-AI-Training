# ADR-005 addendum: rendezvous reachability, co-located today vs. Tailscale multi-host

## Status
Accepted (M5)

## Context
ADR-005 fixes c10d rendezvous as the mechanism for forming a training cohort,
and ADR-005 designates the highest-reliability cohort member as the rendezvous
host. c10d rendezvous needs one property the rest of the system deliberately
does **not** assume: every rank must be able to open an *inbound* TCP connection
to the rendezvous host's endpoint. That is in direct tension with ADR-002, where
agents only ever *dial out* (no inbound reachability, NAT-friendly by
construction) because peers are home laptops behind NAT with no public IP and no
port-forwarding. This addendum records how M5 resolves that tension in the dev
topology and what changes for the real multi-host phase — the resolution is a
real architectural link, not an accident of the dev box.

## Decision / how it works today (co-located, ADR-010 dev)
In the dev topology every cohort container runs on one machine. M5 puts the
cohort's containers on a shared **user-defined Docker bridge network** and gives
the rendezvous-host container a deterministic name derived from the job id
(`gpuorch-rdzv-<job>` / `…-r0`); the orchestrator computes the same
`host_alias:<rendezvous_port>` endpoint in the claim response, so every rank
dials that name and Docker's embedded DNS resolves it to the host container.
This works because "inbound reachability to rank 0" is trivially satisfied when
all ranks share a local L2 network — and it stays honest about ADR-007: a
user-defined bridge is still a bridge, never `--network host`, so `cap_drop=ALL`,
read-only rootfs, and the rest of the isolation profile are unchanged. The high
rendezvous port (29500) needs no `CAP_NET_BIND_SERVICE`. Any result produced
this way is labelled "co-located multi-process" (ADR-010) — one host, not a WAN.

## What changes for the real multi-host phase (Tailscale)
When peers are real NATed laptops, the shared Docker bridge disappears and the
ADR-002 tension becomes real: rank>0 peers genuinely cannot open an inbound
connection to a rank-0 peer sitting behind its own NAT. The project's already-
chosen answer for peer-to-peer connectivity — the **Tailscale overlay** (ADR-010)
— is *exactly* what c10d rendezvous needs: Tailscale gives every peer a stable,
directly-routable overlay IP (100.x.y.z) that any other peer on the tailnet can
reach inbound, punching through NAT for us. So the rendezvous host advertises
its Tailscale IP as the endpoint instead of a Docker DNS name, every rank dials
`<tailscale-ip>:29500` over the overlay, and nothing else in the design moves:
the orchestrator still only needs to be reachable on the overlay (agents still
dial out to it for lifecycle/streams per ADR-002), and the cohort gains the one
capability — inbound reachability *between ranks* — that plain dial-out could not
provide. That is the clean resolution: ADR-002's dial-out keeps the control
plane NAT-friendly, while Tailscale supplies the direct rank-to-rank reachability
the data plane's rendezvous requires, with no public IP or port-forwarding on any
peer. The endpoint string is the only thing that differs between the two
topologies (Docker DNS name vs. overlay IP); the c10d flags, the rank-aware lease
protocol, and the rendezvous-host-is-highest-reliability rule are identical.

## Consequences
- The dev rendezvous path exercises the *real* c10d machinery (real process
  group, real gradient all-reduce), not a stub — only the address resolution
  differs from multi-host, so the multi-host move is additive (swap the endpoint
  source), matching ADR-010's "additive, not a re-architecture" promise.
- The rendezvous host must be a peer that is actually reachable on the overlay;
  since Tailscale makes every enrolled peer reachable, the ADR-005 rule
  (highest-reliability member hosts rendezvous) needs no reachability caveat.
- NCCL vs. gloo is orthogonal to reachability: gloo is used for M5's mixed/CPU
  verification (ADR-005), and the backend choice does not change how ranks find
  each other.
