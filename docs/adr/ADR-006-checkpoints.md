# ADR-006: Checkpoints

## Status
Accepted

## Context
Training runs on unreliable consumer hardware and must be resumable after
a node drops. Checkpoint writes must not race each other from multiple
ranks, and a reader must never observe a partially-written checkpoint.

## Decision
MinIO (S3 API) as checkpoint storage. Only rank 0 writes checkpoints
(other ranks never write, eliminating write races by construction).
Writes are atomic puts (the object either fully exists at its final key or
doesn't appear at all — no partial-object reads), and each checkpoint is
recorded in a versioned manifest that lists checkpoint objects in order
with enough metadata (step, epoch, cohort, timestamp) to identify the
latest valid checkpoint for a resume.

## Consequences
- Single-writer (rank 0) removes the need for distributed write
  coordination on the storage side.
- The manifest is the source of truth for "what's the latest good
  checkpoint," not a listing/glob over the bucket — resuming a job means
  reading the manifest, not guessing from object names.
- S3 API keeps the storage backend swappable (MinIO in dev/self-hosted,
  any S3-compatible provider later) without changing application code.
- Rank-0-only writes mean rank 0 becomes a soft dependency for
  checkpointing specifically; if rank 0 is the node that fails, the next
  rendezvous host (ADR-005) becomes rank 0 for subsequent steps and takes
  over write duty.
