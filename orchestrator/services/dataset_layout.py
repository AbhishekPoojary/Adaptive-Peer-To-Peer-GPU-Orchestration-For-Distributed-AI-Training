"""Normalise a non-standard dataset archive into the layout the validator wants.

:mod:`orchestrator.services.dataset_archive` accepts exactly one shape --
``train/<class>/*.png`` and ``test/<class>/*.png`` -- and it is right to. The
trainer derives its output layer width from the class list, and a loose
validator on data that runs on other people's machines is not a trade worth
making.

The problem is that public datasets almost never arrive that way. The Intel
scene-classification set ships ``seg_train/seg_train/<class>/``; Kaggle's
dogs-vs-cats ships ``train/cat.0.jpg`` with the label in the filename; plenty of
others ship ``training/`` and ``valid/``. Every one of those produced the same
rejection, and the only cure was to hand the uploader a Python script -- which
is exactly the tinkering this project exists to remove.

So this module reads whatever the uploader has and works out the split and the
class from the path::

    seg_train/seg_train/forest/1.jpg   (Intel)  -> train/forest/1.jpg
    wrapper/train/cat/1.jpg                     -> train/cat/1.jpg
    train/cat.0.jpg                    (Kaggle) -> train/cat/cat.0.jpg
    training/cat/1.jpg                          -> train/cat/1.jpg
    seg_pred/*.jpg                              -> dropped (no labels)

Two rules keep this from weakening anything.

**Nothing is bypassed.** Planning re-runs the validator's own path and
file-type refusals, so a zip-slip or symlink entry is refused here too -- it is
never quietly cleaned up into an accepted upload. And the rewritten archive is
handed back to :func:`summarize_dataset_archive` unchanged, so every limit is
enforced against the bytes that actually reach the bucket rather than the bytes
that arrived.

**Nothing is silent.** :attr:`NormalizationPlan.notes` says in plain words what
was renamed, dropped or carved, and the caller shows it to the uploader and
records it on the dataset. The one judgement this module makes on the
uploader's behalf -- carving a held-out split when the archive has none -- is
precisely the judgement ``dataset_archive`` refused to make *silently*. Written
onto the record where every later reader sees it, it is no longer silent.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field

from orchestrator.services.dataset_archive import (
    ALLOWED_IMAGE_SUFFIXES,
    DatasetArchiveError,
    _is_ignorable,
    _reject_non_regular,
    _reject_unsafe_path,
)

#: Directory names meaning "training data", compared after normalisation so
#: ``seg_train``, ``Train-Set`` and ``training`` all match the same rule.
TRAIN_DIR_NAMES: frozenset[str] = frozenset(
    {"train", "segtrain", "training", "trainset", "trainingset", "trainingdata"}
)

#: Held-out data. Validation folders count: an archive shipping ``train/`` and
#: ``valid/`` already has a held-out split the uploader chose, under another
#: name. Renaming that is not the same as inventing one.
TEST_DIR_NAMES: frozenset[str] = frozenset(
    {
        "test",
        "segtest",
        "testing",
        "testset",
        "testdata",
        "val",
        "valid",
        "validation",
        "eval",
        "evaluation",
    }
)

#: Unlabelled prediction folders. Dropped rather than folded into a split: they
#: carry no ground truth, so an accuracy measured over them would be a number
#: with nothing behind it.
IGNORE_DIR_NAMES: frozenset[str] = frozenset(
    {
        "pred",
        "segpred",
        "predict",
        "prediction",
        "predictions",
        "unlabeled",
        "unlabelled",
        "infer",
        "inference",
        "submission",
    }
)

#: Characters allowed in a generated class directory; everything else is
#: replaced. The class name arrives from the uploader's archive and is about to
#: become a path component, so it is rebuilt from an allowlist rather than
#: escaped -- it cannot reintroduce a separator or a ``..`` whatever it held.
_CLASS_SAFE = re.compile(r"[^a-z0-9._-]+")

#: Streaming copy size for the rewrite.
_COPY_CHUNK_BYTES = 1024 * 1024

_SPLITS: tuple[str, str] = ("train", "test")


@dataclass(frozen=True, slots=True)
class NormalizationPlan:
    """A rename map from the uploaded archive to a conformant one.

    ``entries`` is ``(source member name, target path)``. Targets are generated
    here and never copied from the input, which is what makes the rewrite
    immune to path tricks regardless of what the source contained.
    """

    entries: list[tuple[str, str]]
    #: Plain-language account of every change, for the uploader and the record.
    notes: list[str] = field(default_factory=list)
    #: How many images were moved out of train/ into a generated test/ split.
    #: Non-zero means this module chose the held-out set, not the uploader.
    carved_images: int = 0
    #: Declared uncompressed size of the members being kept.
    declared_bytes: int = 0


def _norm(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "").replace(" ", "")


def _safe_class(raw: str) -> str | None:
    """Rebuild ``raw`` into a path component that is safe by construction."""
    cleaned = _CLASS_SAFE.sub("_", raw.strip().lower()).strip("._-")
    return cleaned[:64] or None


def _class_from_filename(basename: str) -> str | None:
    """``'cat.0.jpg' -> 'cat'``; ``None`` when the prefix is absent or numeric.

    This is the flat Kaggle shape, where the only label is the first token of
    the filename stem. A purely numeric prefix (``0001.jpg``) is an index, not
    a label, so it is refused rather than turned into a class called "0001".
    """
    stem = posixpath.splitext(basename)[0]
    for separator in (".", "_", "-"):
        if separator in stem:
            head = stem.split(separator, 1)[0]
            if head and not head.isdigit():
                return head
    return None


def classify(path: str) -> tuple[str | None, str | None]:
    """Work out ``(split, class)`` for one member, or ``(None, None)``.

    The split is the *last* path segment that names one, so a doubled
    ``seg_train/seg_train/`` resolves the same as a single ``train/``, and a
    wrapper directory of any depth is tolerated. The class is the directory
    holding the file -- unless that directory *is* the split, in which case the
    label can only be in the filename.
    """
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if len(parts) < 2:
        return None, None

    split: str | None = None
    split_at = -1
    for index, part in enumerate(parts[:-1]):
        normalised = _norm(part)
        if normalised in IGNORE_DIR_NAMES:
            return None, None
        if normalised in TRAIN_DIR_NAMES:
            split, split_at = "train", index
        elif normalised in TEST_DIR_NAMES:
            split, split_at = "test", index
    if split is None:
        return None, None

    # A directory sits between the split and the file: that directory is the
    # label.
    if len(parts) - 2 > split_at:
        return split, _safe_class(parts[-2])
    # Otherwise the file is directly inside the split folder, so the label, if
    # there is one at all, is in its name.
    return split, _safe_class(_class_from_filename(parts[-1]) or "")


def plan_normalization(
    archive_path: str,
    *,
    max_files: int,
    max_uncompressed_bytes: int,
    test_fraction: float,
) -> NormalizationPlan | None:
    """Describe how to turn ``archive_path`` into a conformant archive.

    Returns ``None`` when the archive holds nothing this module can recognise,
    which leaves the caller free to report the validator's original complaint
    rather than a second, vaguer one of this module's invention.

    Raises :class:`DatasetArchiveError` for exactly the unsafe entries the
    validator refuses: a hostile archive must not become an accepted one by
    passing through here.
    """
    try:
        zf = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile:
        return None

    found: dict[str, dict[str, list[str]]] = {s: defaultdict(list) for s in _SPLITS}
    source_dirs: set[str] = set()
    ignored_dirs: set[str] = set()
    declared_bytes = 0
    dropped_unlabelled = 0
    dropped_non_image = 0

    with zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise DatasetArchiveError(
                f"archive holds {len(infos)} entries, over the {max_files} limit"
            )
        for info in infos:
            name = info.filename
            if _is_ignorable(name):
                continue
            # The validator's own refusals, applied before this module reads a
            # single byte. Normalisation may rearrange a layout; it may never
            # launder an archive that was refused for being dangerous.
            _reject_unsafe_path(name)
            _reject_non_regular(info)
            if info.is_dir():
                continue

            normalised = name.replace("\\", "/")
            base = posixpath.basename(normalised)
            if base.startswith("."):
                continue
            if posixpath.splitext(base)[1].lower() not in ALLOWED_IMAGE_SUFFIXES:
                dropped_non_image += 1
                continue

            split, class_name = classify(normalised)
            if split is None or class_name is None:
                for part in normalised.split("/")[:-1]:
                    if _norm(part) in IGNORE_DIR_NAMES:
                        ignored_dirs.add(part)
                dropped_unlabelled += 1
                continue

            declared_bytes += info.file_size
            if declared_bytes > max_uncompressed_bytes:
                raise DatasetArchiveError(
                    "archive expands to more than "
                    f"{max_uncompressed_bytes // (1024 * 1024)} MiB once extracted"
                )
            found[split][class_name].append(name)
            for part in normalised.split("/")[:-1]:
                if _norm(part) in TRAIN_DIR_NAMES or _norm(part) in TEST_DIR_NAMES:
                    source_dirs.add(part)

    if not found["train"]:
        return None

    notes: list[str] = []
    renamed = sorted(source_dirs)
    if renamed and renamed != ["test", "train"]:
        notes.append(
            "read a non-standard layout: "
            + ", ".join(f"{directory}/" for directory in renamed[:6])
            + " were mapped onto train/ and test/"
        )

    # Carve a held-out split for any class that has none. This is the judgement
    # dataset_archive declined to make silently, so it is counted, named in the
    # notes and written onto the dataset record. Every stride-th file rather
    # than the first N, so both splits sample the whole class instead of test/
    # receiving only whatever happened to sort last.
    stride = max(2, round(1 / test_fraction)) if test_fraction > 0 else 0
    carved = 0
    for class_name, members in found["train"].items():
        if found["test"].get(class_name) or not stride or len(members) < stride:
            continue
        holdout = {m for index, m in enumerate(members) if index % stride == 0}
        found["test"][class_name] = [m for m in members if m in holdout]
        found["train"][class_name] = [m for m in members if m not in holdout]
        carved += len(holdout)

    classes = sorted(set(found["train"]) & set(found["test"]))
    if not classes:
        return None

    entries: list[tuple[str, str]] = []
    for split in _SPLITS:
        for class_name in classes:
            for index, member in enumerate(found[split][class_name]):
                suffix = posixpath.splitext(member)[1].lower()
                entries.append((member, f"{split}/{class_name}/{index:06d}{suffix}"))
    if not entries:
        return None

    dropped_classes = sorted((set(found["train"]) | set(found["test"])) - set(classes))
    if dropped_classes:
        notes.append(
            "dropped "
            + ", ".join(dropped_classes[:6])
            + ": present in only one of the two splits, so it could not be scored"
        )
    if ignored_dirs:
        notes.append(
            "ignored "
            + ", ".join(f"{directory}/" for directory in sorted(ignored_dirs)[:4])
            + ": no labels there to train or score against"
        )
    if dropped_unlabelled:
        notes.append(
            f"skipped {dropped_unlabelled} image(s) with no discoverable label"
        )
    if dropped_non_image:
        notes.append(f"skipped {dropped_non_image} file(s) that were not images")
    if carved:
        notes.append(
            f"no test/ split was supplied, so every {stride}th training image "
            f"({carved} in total) was held out to score against"
        )

    return NormalizationPlan(
        entries=entries,
        notes=notes,
        carved_images=carved,
        declared_bytes=declared_bytes,
    )


def rewrite_archive(
    source_path: str,
    destination_path: str,
    plan: NormalizationPlan,
    *,
    max_uncompressed_bytes: int,
) -> None:
    """Write a conformant archive at ``destination_path`` following ``plan``.

    Copied a chunk at a time, so a multi-gigabyte upload never lands in memory,
    and metered against the bytes actually read rather than the sizes the
    archive declares -- a bomb lies about the latter, and this is the one place
    in the dataset path that decompresses anything at all.
    """
    written = 0
    with (
        zipfile.ZipFile(source_path) as source,
        zipfile.ZipFile(destination_path, "w", zipfile.ZIP_DEFLATED) as destination,
    ):
        for member, target in plan.entries:
            with source.open(member) as reader, destination.open(target, "w") as writer:
                while chunk := reader.read(_COPY_CHUNK_BYTES):
                    written += len(chunk)
                    if written > max_uncompressed_bytes:
                        raise DatasetArchiveError(
                            "archive expands to more than "
                            f"{max_uncompressed_bytes // (1024 * 1024)} MiB "
                            "once extracted"
                        )
                    writer.write(chunk)
