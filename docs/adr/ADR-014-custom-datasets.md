# ADR-014: Custom image-classification datasets

## Status
Accepted.

## Context

Until now `JobSpec.dataset` was `Literal["cifar10", "mnist"]`. Those two are
real datasets and the training on them is real, but a system that can only train
on the two sets baked into its own source is a demo of an orchestrator, not a
tool anyone can use for their own work. The first question a new user asks —
"how do I train on my data?" — had no answer.

The constraint that shapes everything here: **a dataset is executed against on
other people's machines.** Every peer that claims a job downloads and unpacks
whatever the dataset contains, on hardware volunteered by someone who is not the
person who uploaded it. That makes an uploaded archive an attack surface in a way
that a job's epoch count is not, and it is why most of this ADR is about refusal
rather than features.

## Decision

### 1. Image classification only, because that is what the model can do

The trainer implements exactly one architecture, `SmallCNN`, an image classifier.
Supporting tabular or text data would mean new architectures, a new model
registry, and a way to describe input schemas — a much larger change that has
nothing to do with dataset *transport*. Scope is therefore custom **image**
datasets, and the docs say so plainly rather than letting someone discover it by
uploading a CSV.

### 2. ImageFolder-shaped zip, and nothing that can execute

An archive is a zip laid out in the torchvision `ImageFolder` convention:

    train/<class-name>/<any-name>.png
    test/<class-name>/<any-name>.png

Only image extensions are accepted, from an allowlist. This is the decision that
matters most, and the alternatives were rejected on safety:

- **`torch.save` tensors / pickle.** `torch.load` deserializes pickle, which is
  arbitrary code execution by design. Handing a pickle to every volunteer's
  machine and calling it a dataset would be the single worst thing this project
  could ship. Rejected outright.
- **NumPy `.npy` / `.npz`.** Safe *only* with `allow_pickle=False`, which is one
  forgotten keyword away from the same problem, and object arrays make that
  keyword easy to want. Rejected as too sharp an edge for the benefit.
- **Folder of images.** Inert. A `.png` cannot execute; the worst it can do is
  find a decoder bug, which is what ADR-007's container isolation is already for.

### 3. Both splits are required; nothing is auto-split

An archive without `test/` is refused rather than split automatically.

This project reports held-out accuracy as evidence (CONTRIBUTING.md rule 4).
If the orchestrator carved a test set out of `train/` on the uploader's behalf,
every reported number would depend on a decision the reader cannot see and the
uploader never made. That is a quieter version of exactly the fabrication this
repository is organized to prevent.

Accepting `train/` alone and recording `split: "auto"` alongside the number is a
legitimate design — it keeps the provenance visible — and it is **deferred, not
rejected**. It costs a schema field and an honest label. It is not in this change
because requiring the split is simpler and because nobody has yet asked for it.

### 4. The archive travels through MinIO, not the control plane

Uploads go to a `datasets` bucket; peers fetch directly from it with a presigned
URL. Streaming gigabytes through the orchestrator would make the control plane
the bottleneck for the one operation guaranteed to be large, and it already has
MinIO alongside it for checkpoints (ADR-006) — the credentials, the deployment,
and the peer's network path to it all exist.

**The URL is minted per claim, not stored in the spec.** A job's spec is kept
forever; a presigned URL inside it would be a long-lived credential sitting in
the `jobs` table, readable by anyone who can read a job. Minting at claim time
means it expires (`DATASET_URL_TTL_SECONDS`, default 1 h), a retry on another
node gets a fresh one, and no peer ever holds the bucket's actual keys.

### 5. Validate at upload, verify at use

The orchestrator inspects the archive's zip central directory — without
decompressing anything — and refuses path traversal (`../`, absolute paths,
drive-qualified paths), symlink and device entries, non-image files, declared-size
blowup, excessive entry counts, and train/test class mismatches.

Reading only the declared metadata is deliberate: a validator that decompressed
in order to inspect would be vulnerable to the decompression bombs it exists to
catch.

Upload-time validation only means something at the far end if the far end gets
the same bytes, so the archive's SHA-256 is recorded and **the trainer
recomputes it after downloading and refuses to extract on a mismatch**. The
trainer also re-checks entry paths while extracting. That is belt and braces
given the digest already matched, and it stays because `ZipFile.extractall` is
historically the single most common source of zip-slip bugs and the check costs a
string comparison per entry.

### 6. Uploading is ADMIN; using is OPERATOR

Uploading a dataset causes code to run against it on other people's hardware —
the same class of privilege as enrolling a node, which ADR-012 already put behind
ADMIN. It sits behind that existing gate rather than a new one. Any authenticated
user can list and select datasets, because an OPERATOR who cannot see the list
cannot submit a job at all.

### 7. `dataset` and `dataset_id`, exactly one

`JobSpec` keeps `dataset` unchanged and adds an optional `dataset_id`, with a
validator requiring exactly one.

Keeping the built-in field exactly as it was is what lets every existing caller
— the bench harness, the dashboard, 89 historical jobs — keep validating.
`JobSpec` forbids extra fields, so a more elegant redesign (a tagged union, a
`dataset: {type, ref}` object) would have 422'd all of them for no gain.

Neither field set is rejected because there is nothing to train on; both set is
rejected because it is ambiguous, and the resolution would otherwise be decided
silently by whichever branch the trainer happened to check first.

### 8. No augmentation, and declared normalization

Custom images are resized to 64×64, converted to RGB, and normalized with
mean/std 0.5.

The built-in CIFAR-10 path applies random horizontal flips, because that is
known-safe for those classes. An arbitrary upload might be digits, text, or
medical scans, where a flip changes the label. Applying it silently would corrupt
the training signal in a way that surfaces only as a mysteriously poor accuracy
number, so it is not applied.

The 0.5 constants are labelled in the source as a **declared convention**, not
measured statistics — unlike the CIFAR-10 and MNIST constants beside them, which
are published values for those datasets. Computing an upload's real statistics
would need a full pass over the data before training could start. Presenting a
convention as if it were fitted would be the same category of error as
fabricating telemetry.

## Consequences

- New `datasets` table (migration `0012`), a `datasets` MinIO bucket, and four
  endpoints. `jobs` is untouched: the dataset reference lives in the existing
  JSONB `spec`, so every historical job validates exactly as before.
- Deletion is a soft delete. A finished job records which dataset it trained on,
  and dropping the row would leave that job pointing at nothing — turning a real
  training record into an unanswerable question. The stored object *is* removed.
- The `server` extra gains `boto3` (the orchestrator had no S3 client before;
  checkpoints are written by the trainer) and `python-multipart` (Starlette needs
  it to parse an upload at all).
- `create_job` now serializes the spec with `model_dump(mode="json")`. A
  `uuid.UUID` in the spec is not JSON-serializable and asyncpg rejects it
  outright; every other spec field was already a primitive, so nothing about how
  existing specs are stored changes.
- A dataset deleted between submit and claim leaves the granted spec without a
  URL, and the peer fails that lease with a clear reason. The job ends FAILED
  with an explanation rather than being silently never granted.
- **Accepted limitation: the images themselves are not inspected.** Validation
  proves the archive is a tree of files with image extensions and safe paths; it
  does not decode them. A malformed file that exploits a decoder bug would be
  caught by ADR-007's container isolation, not by this. Decoding every image at
  upload would make a large upload cost minutes of orchestrator CPU, and the peer
  runs sandboxed for exactly this class of reason.
- **Accepted limitation: no per-user dataset ownership.** Any ADMIN can delete
  any dataset. The system has two roles and no notion of "my" resources
  (ADR-012 §2); inventing one here would be modelling a hierarchy that does not
  exist elsewhere in the system.
