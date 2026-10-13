# Real-fleet results (12 Oct 2026)

Measured on the live orchestrator (Azure B1s, 1 vCPU, 1 GB RAM, India South
Central) with real volunteer machines. Produced by `bench/real/experiment.py`
and `bench/real/recovery_local.py`; nothing simulated.

## Fleet

| Node | Hardware | Network | Runtime |
|---|---|---|---|
| node-03 | RTX 3050 Laptop 4 GB, 16 cores | phone hotspot | Docker |
| node-02 | CPU only, 12 cores | phone hotspot | Docker |
| node-08 | RTX 3050 Laptop 6 GB, 16 cores | lab LAN | Docker (morning only) |
| node-06, node-07 | CPU only, 12 cores | lab LAN | no Docker (morning only) |

Placement, recovery and scaling used node-03 and node-02 only.

## Conditions that matter when reading the numbers

- **Failure detector**: `PHI_ACCRUAL_ACCEPTABLE_PAUSE_SECONDS=8.0` (default 2.0).
  At 2.0, hotspot jitter and server stalls caused repeated false failures; see
  `20261012-heartbeats.csv` for the real inter-arrival data behind the change.
- **Server memory**: `vm.swappiness=10` from 09:40 UTC; before it, swap stalls
  on the 1 GB server delayed every node's heartbeats at once.
- **Lab PCs**: CPU training starved the agent's heartbeats until the trainer
  ran below normal priority (commit 3323b68); the lab PCs left before they
  picked that fix up, so no lab result after it exists yet.
- **Speed term**: learned from a learning round (`*-scaling-learn*`) before the
  placement run; GPU ~6,000 images/s, CPU ~500 images/s (CIFAR-10, SmallCNN).
- **Round-robin in `placement`**: schedulers were interleaved one job at a
  time, so round-robin's turn always came after six GPU placements and picked
  the CPU node every time. In steady state it would alternate.
- **Recovery timings** subtract this PC's kill time from the server's event
  times: two clocks, up to about a second apart.

## Headline numbers

- Placement (CIFAR-10, n=10 each): adaptive 21.2 ± 3.9 s; without the speed
  term 53.6 ± 41.1 s (p=0.027); round-robin 90.2 ± 11.0 s (p=0.0001);
  least-loaded 21.2 ± 2.8 s (no difference on this two-node fleet).
- Recovery (n=10): detected 10.7 ± 3.7 s, restarted 13.4 ± 3.6 s, resumed
  from checkpoint 16.4 ± 3.5 s after the kill; 10/10 resumed from a checkpoint.
- Scaling (12 jobs): GPU alone 235 s; GPU + CPU 340 s -- the CPU node's
  ~100 s jobs straggle. Completion-time-aware placement is the planned fix.

## 13 Oct 2026: completion-time-aware placement

Same two laptops; friend's hotspot unstable all day (failed checkpoint uploads,
lease losses), this PC's hotspot dropped for >30 s once.

| Run | Machines | 12-job makespan | Extra attempts | Valid |
|---|---|---|---|---|
| adaptive (min completion time), r1 | 2 | 243 s | 2 | yes |
| adaptive_no_defer (greedy), r1 | 2 | 287 s | 3 | yes |
| adaptive, r2 | 1 (node-02 offline) | 233 s | 1 | no: one machine |
| adaptive_no_defer, r2 | 2 | 1269 s | 6 | no: network churn |

Before the overrun fix (commit 56c548f) the first version held the queue
behind a job that overran its estimate: 639 s for the same 12 jobs (run
discarded; number kept here as the evidence for the fix). To repeat on the lab
LAN with more machines before drawing a statistical conclusion.
