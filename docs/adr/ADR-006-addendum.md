# ADR-006 addendum (M6): checkpoint keys, manifest schema, resume

## Status
Accepted (extends ADR-006)

## Context
ADR-006 fixed the *policy* — MinIO/S3, rank-0-only writes, atomic puts, a
versioned manifest as the source of truth — but not the concrete key layout,
the manifest JSON, the write protocol's exact ordering, or the resume contract.
M6 implements the first real use of MinIO, so those are pinned here.

## Where the state lives: MinIO, not Postgres
The manifest is **pure MinIO-side state** (an S3 object), exactly as ADR-006
says: "the manifest is the source of truth … not a listing/glob over the
bucket." The orchestrator tracks **no** checkpoint rows — deliberately:

* it keeps the storage backend swappable behind the S3 API with zero schema
  coupling (ADR-006);
* the writer is the trainer container, which already talks S3 for the blob — a
  second write to Postgres would need a new epoch-fenced agent endpoint for no
  correctness gain;
* resume is a trainer-startup concern (read the manifest for this `job_id`), not
  an orchestrator scheduling input.

Consequence: **M6 adds no Alembic migration.** `TrainingMetric.step` (nullable,
already in the M4 schema) is reused to record the resumed step on the metric
curve. The migration chain is still verified up/down clean as a regression.

## Key layout (bucket `S3_BUCKET_CHECKPOINTS`, default `checkpoints`)
```
manifests/<job_id>.json                                  # the source of truth
checkpoints/<job_id>/e<epoch:03d>-s<step:08d>-<uuid8>.pt  # one blob per write, immutable
```
Each blob key is unique (uuid8 suffix), so a write never overwrites a prior
checkpoint and an interrupted write cannot corrupt an existing object.

## Manifest schema (v1)
```jsonc
{
  "schema": 1,
  "job_id": "<uuid>",
  "updated_utc": "<iso8601 Z>",
  "latest": <entry>,              // == checkpoints[-1]; the resume target
  "checkpoints": [ <entry>, ... ] // append-only, oldest → newest
}
// entry:
{
  "key": "checkpoints/<job_id>/e003-s00001840-1a2b3c4d.pt",
  "step": 1840,                   // global optimizer steps completed
  "epoch": 3,                     // next epoch to run is this value
  "world_size": 1,                // cohort size that wrote it (ADR-006 "cohort")
  "lease_epoch": 2,               // orchestrator attempt epoch that wrote it (audit)
  "timestamp_utc": "<iso8601 Z>",
  "loss": 0.4213                  // last train loss, or null
}
```

## Write protocol (atomic put + manifest, in this exact order)
`save_checkpoint(store, job_id, meta, blob_bytes)`:
1. **PUT the blob** at its unique versioned key. S3 object creation is atomic:
   the object is fully readable at its final key or absent — no partial-object
   reads (ADR-006).
2. **Only if (1) succeeded**, read-modify-write the manifest: fetch the current
   manifest (or start an empty one), append the entry, set `latest`, PUT it back
   at `manifests/<job_id>.json`.

If the blob PUT raises, the manifest is never touched → it keeps pointing at the
previous good checkpoint, never a partial one. If the process dies *between* (1)
and (2), the blob is an orphan the manifest doesn't reference — harmless; resume
reads the manifest and ignores it. This ordering is the invariant the atomicity
test (`tests/test_checkpoint_manifest.py`) pins: a failed blob upload never
advances the manifest.

The manifest read-modify-write is race-free **by ADR-006's single-writer
guarantee**: only rank 0 writes, and after a reassignment the previous attempt's
rank 0 is already dead (hard-killed) before the new attempt's rank 0 starts, so
there is never more than one manifest writer for a `job_id` in flight.

## Resume contract (`trainer/train.py`)
At startup, if checkpointing is configured (`S3_ENDPOINT_URL` + creds present),
every rank reads `manifests/<job_id>.json` (reads are safe for all ranks;
ADR-006 only restricts *writes* to rank 0):

* **manifest present** → download `latest.key`, `torch.load` it, restore model +
  optimizer state, set `start_epoch = latest.epoch`, `global_step = latest.step`,
  and emit `{"type":"resume", ...}` on stdout (the agent turns this into the
  `"Resumed from checkpoint at step N"` JobEvent). Training continues from there.
* **no manifest** (first attempt) → start fresh, exactly as M4/M5.

This makes resume a genuine state restore: the loss right after resume tracks
the loss right before the kill (the model/optimizer are the killed run's), not
the cold-start loss — the discontinuity check in the M6 chaos run proves it.

`CHECKPOINT_EVERY_N_STEPS` (default 100) controls cadence; rank 0 also writes one
checkpoint at the end of each epoch so at most one epoch of work is ever lost.
S3 config flows orchestrator env → agent CLI/env → trainer container env, the
same path the existing `DATASET`/`EPOCHS`/… env vars already take through
`agent.runtime.docker_launcher.build_run_kwargs`.
