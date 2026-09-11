"""Normalise an image dataset zip into the layout the orchestrator requires.

The orchestrator accepts exactly one shape::

    train/<class>/<anything>.jpg
    test/<class>/<anything>.jpg

Public datasets almost never arrive that way, and each variation produces the
same unhelpful rejection. Rather than teach people the required layout, this
reads whatever they have and works out the split and the class from the path:

    seg_train/seg_train/forest/1.jpg   (Intel) -> train/forest/1.jpg
    wrapper/train/cat/1.jpg                    -> train/cat/1.jpg
    train/cat.0.jpg                   (Kaggle) -> train/cat/cat.0.jpg
    training/cat/1.jpg                         -> train/cat/1.jpg
    seg_pred/*.jpg                             -> dropped (no labels)

Images with no discoverable label are dropped rather than guessed at, and a
missing or unusable test split is carved out of train/ so reported accuracy is
still measured on held-out data.

Usage:  python reshape_dataset.py <input.zip> [output.zip] [--test-frac 0.2]
"""

from __future__ import annotations

import posixpath
import sys
import zipfile
from collections import defaultdict

IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".ppm", ".pgm"
}

#: Directory names that mean "training data", normalised to lowercase with
#: separators stripped, so `seg_train`, `Train-Set` and `training` all match.
TRAIN_NAMES = {"train", "segtrain", "training", "trainset", "trainingset"}
#: Held-out data. Validation folders count: a dataset that ships train/ and
#: valid/ has a perfectly good held-out split under another name.
TEST_NAMES = {"test", "segtest", "testing", "testset", "val", "valid",
              "validation", "eval", "evaluation"}
#: Unlabeled prediction folders. Dropped rather than treated as data -- they
#: carry no ground truth, so accuracy measured on them would be meaningless.
IGNORE_NAMES = {"pred", "segpred", "predict", "prediction", "predictions",
                "unlabeled", "unlabelled", "infer", "inference"}


def _norm(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "").replace(" ", "")


def _class_from_filename(basename: str) -> str | None:
    """'cat.0.jpg' -> 'cat'. None when the prefix is absent or just a number."""
    stem = posixpath.splitext(basename)[0]
    for sep in (".", "_", "-"):
        if sep in stem:
            head = stem.split(sep, 1)[0].strip().lower()
            if head and not head.isdigit():
                return head
    return None


def classify(path: str) -> tuple[str | None, str | None]:
    """Work out ``(split, class)`` for one archive member, or ``(None, None)``.

    The split is the last path segment that names one, so a doubled
    ``seg_train/seg_train/`` resolves the same as a single one. The class is
    the directory holding the file, unless that directory *is* the split -- in
    which case the label can only be in the filename, which is the flat Kaggle
    shape.
    """
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if len(parts) < 2:
        return None, None

    split = None
    split_at = -1
    for index, part in enumerate(parts[:-1]):
        normalised = _norm(part)
        if normalised in IGNORE_NAMES:
            return None, None
        if normalised in TRAIN_NAMES:
            split, split_at = "train", index
        elif normalised in TEST_NAMES:
            split, split_at = "test", index
    if split is None:
        return None, None

    parent = parts[-2]
    if len(parts) - 2 > split_at:
        return split, parent.strip().lower()
    return split, _class_from_filename(parts[-1])


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    src = argv[0]
    dst = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else "dataset_ready.zip"
    test_frac = 0.2
    if "--test-frac" in argv:
        test_frac = float(argv[argv.index("--test-frac") + 1])

    found: dict[str, dict[str, list[str]]] = {"train": defaultdict(list), "test": defaultdict(list)}
    dropped_unlabeled = 0
    dropped_ignored = 0

    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            base = posixpath.basename(name)
            if base.startswith(".") or name.lower().startswith("__macosx/"):
                continue
            if posixpath.splitext(base)[1].lower() not in IMAGE_SUFFIXES:
                continue
            split, klass = classify(name)
            if split is None:
                dropped_ignored += 1
            elif klass is None:
                dropped_unlabeled += 1
            else:
                found[split][klass].append(info.filename)

    if not found["train"]:
        print("ERROR: could not find any labelled training images.")
        print("Looked for a folder named train/ (or seg_train, training, ...)")
        print("containing one sub-folder per class.")
        return 1

    # A test split is only usable if it covers the same classes as train. Where
    # it does not -- unlabeled, absent, or partial -- carve the missing classes
    # out of train, taking every Nth file so both splits sample the whole set
    # rather than test getting only whatever sorted last.
    stride = max(2, round(1 / test_frac)) if test_frac > 0 else 0
    carved = 0
    for klass, members in found["train"].items():
        if found["test"].get(klass):
            continue
        if not stride or len(members) < stride:
            continue
        holdout = [m for i, m in enumerate(members) if i % stride == 0]
        found["test"][klass] = holdout
        found["train"][klass] = [m for m in members if m not in set(holdout)]
        carved += len(holdout)

    classes = sorted(set(found["train"]) & set(found["test"]))
    if len(classes) < 2:
        print(f"ERROR: need at least 2 classes with both train and test images; found {len(classes)}.")
        return 1

    with zipfile.ZipFile(src) as zf, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
        written = {"train": {}, "test": {}}
        for split in ("train", "test"):
            for klass in classes:
                members = found[split][klass]
                written[split][klass] = len(members)
                for index, member in enumerate(members):
                    suffix = posixpath.splitext(member)[1].lower()
                    out.writestr(f"{split}/{klass}/{index:06d}{suffix}", zf.read(member))

    print(f"wrote {dst}")
    print(f"  classes: {', '.join(classes)}")
    for split in ("train", "test"):
        total = sum(written[split].values())
        detail = ", ".join(f"{c}={written[split][c]}" for c in classes)
        print(f"  {split}: {total} images ({detail})")
    if carved:
        print(f"  carved {carved} held-out image(s) out of train/")
    if dropped_ignored:
        print(f"  dropped {dropped_ignored} file(s) from unlabeled/prediction folders")
    if dropped_unlabeled:
        print(f"  dropped {dropped_unlabeled} file(s) with no discoverable label")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
