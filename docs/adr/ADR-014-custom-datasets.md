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

> **Amended 2026-09-11 — this is now the behaviour.** Somebody asked: a
> collaborator with no GPU, uploading through the browser from another machine,
> could not produce a conformant archive and could not run a script to make one.
> See *Amendment: layout normalisation* below. The reasoning above is unchanged
> and is what the amendment is built to satisfy — the carve is recorded on the
> dataset rather than left invisible. The "honest label" turned out to cost a
> line on the record rather than a schema field.

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

## Amendment: layout normalisation (2026-09-11)

### What changed

`POST /datasets` no longer refuses an archive solely for the shape of its
directories. A rejected archive is rearranged into `train/<class>/` and
`test/<class>/` and re-validated; only if that fails does the uploader get the
original complaint. `orchestrator/services/dataset_layout.py` holds the
inference, `DATASET_NORMALIZE_LAYOUT=false` turns it off.

### Why the original decision did not survive contact

Decision 3 above reasoned entirely about *honesty*, and that reasoning is
sound. It assumed, silently, that the person who uploads a dataset is the
person who can reshape one. That assumption broke the first time the system did
what it exists to do: a collaborator with no GPU, uploading through a browser
from another machine, had an archive in the Intel scene-classification layout
(`seg_train/seg_train/<class>/`) and no way to rewrite a zip. The only remedy
on offer was "run this Python script", handed to the one participant chosen
precisely because they could not run things locally.

The rejection was also uninformative in the way that matters least: it named
the required layout, which the uploader could already read, and not the
distance between that and what they had.

### How the honesty requirement is kept

Three properties, in priority order:

1. **Nothing is bypassed.** The rewritten archive is passed back to
   `summarize_dataset_archive` unchanged, so every limit — entry count, class
   bounds, decompressed size, allowed extensions — is enforced on the bytes
   that reach the bucket rather than the bytes that arrived. Normalisation can
   only ever produce an archive the validator would have accepted anyway.
2. **A dangerous archive is refused, not repaired.** Planning re-runs
   `_reject_unsafe_path` and `_reject_non_regular` before reading anything, so
   a zip-slip path or a symlink entry raises rather than being quietly dropped
   on the way to a rewrite. This matters more here than anywhere else in the
   dataset path: normalisation runs *only* on archives that were just
   rejected, so a version that skipped hostile entries instead of refusing
   them would have converted every rejection into an acceptance.
3. **The carve is on the record.** When an archive supplies no test split, one
   is taken from `train/` — every stride-th image, so both splits sample the
   whole class — and the fact is written into the dataset's description, shown
   in the upload response, and logged. A reader who finds an accuracy figure
   can find out that the split behind it was chosen by the server. That is the
   "honest label" decision 3 asked for; it cost a line on an existing column
   rather than a new one.

### Consequences

- `reshape_dataset.py` stops being a prerequisite and becomes a diagnostic.
- The stored archive is no longer always byte-identical to the uploaded one, so
  `size_bytes` and `sha256` describe what was stored. They always had to — the
  peer verifies that digest against what it downloads — but it was previously
  impossible to tell the two apart.
- Normalisation is the one place in the dataset path that decompresses
  anything, so the copy meters the bytes it actually reads rather than the
  sizes the archive declares. A bomb lies about the latter.
- A train-only class with fewer images than the carve stride is dropped rather
  than split, since a held-out set of zero is not one. An archive small enough
  for this to remove every class still gets the original rejection.
