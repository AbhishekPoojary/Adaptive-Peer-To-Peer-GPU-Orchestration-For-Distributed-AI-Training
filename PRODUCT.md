# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary: someone with a dataset and no GPU.** They sign in, upload an image
set, wait, and download a trained model. They do not know what a lease, an
epoch, a cohort or a rendezvous host is, and reaching a trained model must not
require learning. Confirmed as the user whose success defines the product.

**The GPU volunteer — a first-class audience with no surface yet.** A stranger
lending their machine needs to see what is running on it, consent to it, and
stop it. Today their entire experience is an install command and the agent's
terminal output; the dashboard represents them only as a node someone else
watches. This is a confirmed gap in what the product owes its users, not a
decision to leave them out.

**The operator.** Runs the orchestrator, watches machines arrive and vanish,
diagnoses why a job failed, and reads the scheduler's reasoning. Served today by
Overview, Nodes, Jobs and the per-job scheduling audit. Backed by the `ADMIN` /
`OPERATOR` role split (ADR-012); uploading a dataset and enrolling a node are
both `ADMIN`, because both cause code to run on other people's hardware.

## Product Purpose

Train real AI models on a pool of ordinary computers — laptops, desktops,
gaming PCs — contributed by different people over the internet, so that having
a dataset does not require owning or renting a GPU.

Success is a stranger with idle hardware and a stranger with a dataset
completing a real training run together, each trusting the system enough to
take part. That is the bar the product is built to: **a tool people will
actually run**, not a demonstration that it could be built.

## Positioning

**The machines call us; we never call them.** A volunteer's agent dials out to
the orchestrator and asks for work, so a home router needs no port forwarding,
no public IP and no firewall change. Every design that requires reaching *into*
a volunteer's machine is unavailable to this product by construction — which is
the constraint the rest of the architecture is shaped by, and the reason a
pool of home machines is usable at all.

Two things follow that a neighbouring product could not truthfully copy without
adopting the same constraint:

- **Assignment is a lease the peer claims, not a task pushed to it**, with epoch
  fencing so a machine that wakes up after being declared dead cannot corrupt
  work that moved on.
- **Scheduling is adaptive and auditable.** Nodes are ranked on measured latency,
  observed reliability (Wilson lower bound with time-decayed evidence) and
  current demand, and every decision is recorded per job with its inputs, so the
  choice can be inspected rather than trusted.

## Operating Context

- One command on the host (`demo.ps1`) starts Postgres, MinIO, the orchestrator,
  the local agent and the dashboard, deriving the LAN address on each run
  because a stale one silently breaks dataset downloads.
- A collaborator on another network reaches the dashboard through a Cloudflare
  quick tunnel. Quick tunnels are ephemeral, rate-limited, cut off any single
  long request, and are a demo transport rather than a deployment.
- A volunteer installs the agent with a one-line command carrying a
  short-lived enrollment token, then leaves it running. Machines are expected to
  come and go mid-run; φ-accrual failure detection and lease expiry handle it.
- Training runs in a Docker container on the volunteer's machine when Docker is
  present, and unsandboxed with explicit consent when it is not.
- Uploaded datasets are validated before storage, extracted on someone else's
  hardware, and verified by SHA-256 there before extraction.

**Terminology** (used consistently in code, docs and UI): orchestrator, agent,
node, job, lease, lease epoch, cohort, rendezvous host, enrollment token,
dataset, checkpoint.

## Capabilities and Constraints

**Confirmed capabilities**

- Image classification only. One architecture (`SmallCNN`); every custom-dataset
  image is resized to `CUSTOM_IMAGE_SIZE` (64px) before training.
- Built-in CIFAR-10 and MNIST, plus uploaded datasets.
- Uploads accept any common directory layout and are rearranged server-side;
  large archives upload in resumable chunks and are shrunk to training
  resolution in the browser first. Every transformation is recorded on the
  dataset.
- Checkpoint, resume and model download; per-job event timeline, metrics,
  training logs and scheduling audit.

**Durable constraints**

- **No inbound connection to a volunteer is ever possible.** See Positioning.
- Multi-node training (`world_size > 1`) needs genuine peer-to-peer
  reachability, which no reverse tunnel provides. Single-node is the working
  case; cohorts across NAT are explicitly out of scope.
- **No TLS outside an overlay network.** Bearer tokens would cross any other
  network in plaintext, and two user-management routes now carry plaintext
  passwords. Anything beyond the overlay needs TLS terminated in front. This is
  the largest open risk against "a tool strangers will run".
- Token revocation is bounded by TTL only (15 minutes default); disabling an
  account takes effect immediately, but a stolen token stays valid until expiry.
- The rate limiter is per-process, so N replicas allow N times the limit.
- A node can never be removed, so a long-lived fleet list accumulates dead
  machines and Overview's denominator grows with them.

**Explicitly undecided**

- Whether the volunteer's surface is web, native or in-agent.
- Whether a public deployment gets TLS via a reverse proxy or stays
  overlay-only.

## Brand Commitments

- Name: `gpu-orchestrator` in the repository and `GPU Orchestrator` in the
  interface. No logo, wordmark or supplied visual asset exists.
- **Voice is binding on the interface, not only the docs.** Plain words over
  jargon; say why rather than only what; never imply a number, a state or a
  certainty that was not measured. The interface already holds this line — an
  unreported metric reads "No data reported yet" rather than a zero — and future
  copy, empty states and error messages are held to the same standard.
- **Standing visual preference: the modern product-SaaS idiom, executed
  straight.** Recorded 2026-09-16 after the owner reviewed a dealt hand of
  distinctive directions and chose the familiar path deliberately. Craft bar is
  **Stripe and Resend** — light ground, generous whitespace, typography doing
  the work, motion used sparingly and purposefully. Ground is light with a cool
  tint; primary navigation is a top horizontal pill row rather than a sidebar.
  Future surfaces inherit this rather than reopening the question, and it is a
  commitment to *craft level*, not licence to ship the category default
  carelessly.

## Evidence on Hand

**Real and available**

- `docs/adr/` — numbered decision records, several carrying dated amendments
  that state what was superseded and why.
- `bench/` — benchmark harness, scenarios, JSON schema and machine-written
  reports. Every benchmark number is written by a machine to a timestamped
  artifact, never typed.
- `docs/screenshots/` — real captures of Overview, Nodes, Submit and Job detail
  at 1440px and 375px.
- `docs/STATUS.md` — current state and an explicit known-gaps list.
- A completed real training run: a 6-class scene dataset (14,034 train / 3,000
  test), 86.4% held-out accuracy over 10 epochs on `cuda`, with a downloadable
  1.6 MB checkpoint and a full metric-per-epoch record.

**Absences future work must not fill in**

- **The usability test has never been run with a real person.**
  `docs/USABILITY-TEST.md` is a complete script awaiting someone who has not
  seen the system. `docs/STATUS.md` states plainly: *do not simulate it — an
  invented finding is worse than an untested interface, because it looks like
  evidence.* Every claim about what a first-time user finds confusing is
  currently a hypothesis.
- No users, customers, testimonials, press, pricing, uptime figures or adoption
  numbers exist. None may be invented, implied or illustrated.

## Product Principles

1. **Nothing on screen may be more certain than the measurement behind it.** The
   anti-fabrication law (`CONTRIBUTING.md`) governs the interface as much as the
   telemetry: no placeholder that reads as data, no zero standing in for unknown,
   no plausible number where a real one is missing.
2. **The machines call us; we never call them.** Any design needing inbound
   access to a volunteer is not available to this product.
3. **A peer is a stranger, and gets the least authority that lets it work.** No
   storage credentials leave the orchestrator; tokens are scoped to one job and
   one audience; a dataset is proved to be a plain tree of images before it can
   run on anyone else's machine.
4. **Someone with a dataset must reach a trained model without learning the
   system's vocabulary.** Leases, epochs and cohorts are the operator's
   language; they must never be the price of entry.
5. **An admitted gap beats an invented answer.** Where evidence is missing, the
   product says so — in docs, in the UI, and in this record.

## Accessibility & Inclusion

**Target: WCAG 2.1 AA.** A standing constraint on every future surface —
contrast ratios, full keyboard operability, visible focus, and screen-reader
labelling. Not yet audited against that target; the commitment is recorded here
as the bar to meet, not as a claim already satisfied.
