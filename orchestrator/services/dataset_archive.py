"""Validation of an uploaded dataset archive (ADR-014).

Every byte inspected here ends up being extracted on **somebody else's laptop**.
A peer runs whatever a job's dataset contains, so this module treats the archive
as hostile input and refuses anything it cannot prove is a plain tree of images.

Layout required
---------------
A zip whose entries form the torchvision ``ImageFolder`` convention::

    train/<class-name>/<any-name>.png
    test/<class-name>/<any-name>.png

Both splits are mandatory. The project reports held-out accuracy as evidence
(CONTRIBUTING.md rule 4), so the held-out set has to be something the submitter
chose deliberately. Silently carving a test split out of ``train/`` would make
every reported number depend on a decision the reader cannot see, which is the
kind of quiet fabrication this repository exists to avoid. Accepting ``train/``
alone and recording ``split: auto`` is a real option, deferred rather than
rejected — see the ADR.

What is rejected, and why each one matters
------------------------------------------
=============================  ===============================================
Absolute or ``..`` paths       "Zip slip": an entry named ``../../etc/cron.d/x``
                               escapes the extraction directory and writes
                               anywhere the trainer process can reach.
Symlinks / non-regular files   A symlink entry pointing at ``/etc/passwd`` turns
                               a later write into a write *through* the link,
                               and the zip format happily stores them.
Non-image extensions           A ``.py`` beside the images is only inert until
                               something globs the directory. The trainer has no
                               reason to accept anything but images, so it does
                               not.
Declared size blowup           A zip bomb is kilobytes on the wire and terabytes
                               on disk. Checked against the *declared*
                               uncompressed sizes before a single byte is
                               extracted.
Too many entries               Bounds the validation walk itself.
Mismatched classes             A class in ``test/`` absent from ``train/`` means
                               the model is scored on a label it was never
                               taught — an accuracy number that cannot mean what
                               it appears to.
=============================  ===============================================

Only the *declared* metadata is read here; nothing is decompressed. That keeps
validation cheap and keeps this module free of the decompression bombs it is
meant to detect. The peer re-verifies the archive's SHA-256 before extracting,
so what is validated here is provably what runs there.
"""

from __future__ import annotations

import posixpath
import zipfile
from collections import Counter
from dataclasses import dataclass, field

#: Extensions Pillow reads and torchvision's ImageFolder accepts by default.
#: An allowlist, not a denylist: anything unrecognized is refused rather than
#: assumed harmless.
ALLOWED_IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".ppm", ".pgm"}
)

#: The two splits an archive must provide.
REQUIRED_SPLITS: tuple[str, str] = ("train", "test")

#: Archive members that are metadata rather than data, and are ignored instead of
#: failing the upload. macOS adds __MACOSX/ and .DS_Store to any zip made in
#: Finder, and a dataset rejected for that reason would be baffling.
_IGNORED_BASENAMES: frozenset[str] = frozenset({".ds_store", "thumbs.db"})
_IGNORED_PREFIXES: tuple[str, ...] = ("__macosx/",)

#: Unix file-type bits (stat.S_IFMT) for a regular file and a directory. Anything
#: else in an archive entry's stored mode is refused.
_S_IFMT = 0o170000
_S_IFREG = 0o100000
_S_IFDIR = 0o040000


class DatasetArchiveError(Exception):
    """The archive is not a usable dataset. The message is shown to the uploader.

    Unlike an auth failure, being specific here is the whole point: the person
    holding the file is the only one who can fix it, and "invalid archive" tells
    them nothing about which of a dozen requirements they missed.
    """


@dataclass(frozen=True, slots=True)
class SplitSummary:
    """Per-split counts, derived by walking the archive's entry names."""

    images: int
    #: class name -> image count, so a wildly imbalanced upload is visible in the
    #: UI before someone spends an hour training on it.
    per_class: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DatasetArchiveSummary:
    """What the archive contains, once proved to contain only what it should."""

    classes: list[str]
    train: SplitSummary
    test: SplitSummary
    total_uncompressed_bytes: int

    @property
    def num_classes(self) -> int:
        return len(self.classes)

    @property
    def total_images(self) -> int:
        return self.train.images + self.test.images


def _is_ignorable(name: str) -> bool:
    lowered = name.lower()
    if any(lowered.startswith(prefix) for prefix in _IGNORED_PREFIXES):
        return True
    return posixpath.basename(lowered) in _IGNORED_BASENAMES


def _reject_unsafe_path(name: str) -> None:
    """Refuse any member name that could write outside the extraction root."""
    if name.startswith("/") or name.startswith("\\"):
        raise DatasetArchiveError(
            f"archive contains an absolute path ({name!r}); "
            "all entries must be relative"
        )
    # Windows drive letters ("C:foo") are absolute to some extractors even
    # without a leading separator.
    if len(name) >= 2 and name[1] == ":":
        raise DatasetArchiveError(f"archive contains a drive-qualified path ({name!r})")
    # Normalizing first means "a/../../b" is caught as well as a bare "../b".
    normalized = posixpath.normpath(name.replace("\\", "/"))
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        raise DatasetArchiveError(
            f"archive entry {name!r} escapes the extraction directory"
        )


def _reject_non_regular(info: zipfile.ZipInfo) -> None:
    """Refuse symlinks, devices, and anything else that is not a plain file."""
    # The high 16 bits of external_attr carry the Unix mode when the archive was
    # created on a Unix system. Branch on the *file-type* bits specifically, not
    # on the mode being non-zero: a zip made on Windows stores no type bits at
    # all, and zipfile.writestr stores permission bits (0o600) with none either.
    # Testing `mode` alone rejected both — that is, nearly every real archive.
    file_type = (info.external_attr >> 16) & _S_IFMT
    if file_type and file_type not in (_S_IFREG, _S_IFDIR):
        raise DatasetArchiveError(
            f"archive entry {info.filename!r} is not a regular file or directory "
            "(symlinks and device nodes are refused)"
        )


def summarize_dataset_archive(
    archive_path: str,
    *,
    max_files: int,
    max_uncompressed_bytes: int,
    min_classes: int,
    max_classes: int,
) -> DatasetArchiveSummary:
    """Validate ``archive_path`` and describe what it holds.

    Raises :class:`DatasetArchiveError` with a message intended for the uploader.
    Reads only the central directory — nothing is decompressed.
    """
    try:
        zf = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise DatasetArchiveError("that file is not a readable .zip archive") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise DatasetArchiveError(
                f"archive holds {len(infos)} entries, over the {max_files} limit"
            )

        declared_bytes = 0
        counts: dict[str, Counter[str]] = {s: Counter() for s in REQUIRED_SPLITS}
        saw_unknown_top_level: set[str] = set()

        for info in infos:
            name = info.filename
            if _is_ignorable(name):
                continue
            _reject_unsafe_path(name)
            _reject_non_regular(info)
            if info.is_dir():
                continue

            declared_bytes += info.file_size
            if declared_bytes > max_uncompressed_bytes:
                raise DatasetArchiveError(
                    "archive expands to more than "
                    f"{max_uncompressed_bytes // (1024 * 1024)} MiB once extracted"
                )

            parts = posixpath.normpath(name.replace("\\", "/")).split("/")
            # Tolerate a single wrapping directory, which is what you get by
            # zipping a folder rather than its contents — an extremely common
            # way to produce an otherwise perfect archive.
            if len(parts) >= 4 and parts[1] in REQUIRED_SPLITS:
                parts = parts[1:]
            if len(parts) != 3:
                if parts and parts[0] not in REQUIRED_SPLITS:
                    saw_unknown_top_level.add(parts[0])
                continue

            split, class_name, filename = parts
            if split not in REQUIRED_SPLITS:
                saw_unknown_top_level.add(split)
                continue

            suffix = posixpath.splitext(filename)[1].lower()
            if suffix not in ALLOWED_IMAGE_SUFFIXES:
                raise DatasetArchiveError(
                    f"{name!r} is not an image. Allowed types: "
                    + ", ".join(sorted(ALLOWED_IMAGE_SUFFIXES))
                )
            counts[split][class_name] += 1

    train_classes = set(counts["train"])
    test_classes = set(counts["test"])

    if not train_classes and not test_classes:
        hint = ""
        if saw_unknown_top_level:
            found = ", ".join(sorted(saw_unknown_top_level)[:5])
            hint = f" Found top-level entries instead: {found}."
        raise DatasetArchiveError(
            "archive has no train/<class>/image files. Expected "
            "train/<class-name>/*.png and test/<class-name>/*.png." + hint
        )
    for split in REQUIRED_SPLITS:
        if not counts[split]:
            raise DatasetArchiveError(
                f"archive has no {split}/ split. Both train/ and test/ are "
                "required so reported accuracy is measured on data you chose to "
                "hold out."
            )

    if train_classes != test_classes:
        detail = []
        only_train = sorted(train_classes - test_classes)
        only_test = sorted(test_classes - train_classes)
        if only_train:
            detail.append(f"only in train/: {', '.join(only_train[:5])}")
        if only_test:
            detail.append(f"only in test/: {', '.join(only_test[:5])}")
        raise DatasetArchiveError(
            "train/ and test/ declare different classes (" + "; ".join(detail) + ")"
        )

    classes = sorted(train_classes)
    if len(classes) < min_classes:
        raise DatasetArchiveError(
            f"a dataset needs at least {min_classes} classes; found {len(classes)}"
        )
    if len(classes) > max_classes:
        raise DatasetArchiveError(
            f"a dataset may declare at most {max_classes} classes; found {len(classes)}"
        )

    return DatasetArchiveSummary(
        classes=classes,
        train=SplitSummary(
            images=sum(counts["train"].values()), per_class=dict(counts["train"])
        ),
        test=SplitSummary(
            images=sum(counts["test"].values()), per_class=dict(counts["test"])
        ),
        total_uncompressed_bytes=declared_bytes,
    )
