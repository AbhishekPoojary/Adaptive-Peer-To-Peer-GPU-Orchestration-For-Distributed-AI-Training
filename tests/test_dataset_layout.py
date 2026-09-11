"""Server-side layout normalisation.

Two questions are asked of every case here. Does the rearranged archive pass
``summarize_dataset_archive`` -- the real validator, not a stand-in -- and does
rearranging it fail to weaken any refusal the validator makes? The second is
the load-bearing one: normalisation runs on archives that were *just rejected*,
so an implementation that quietly repaired a hostile zip instead of refusing it
would be strictly worse than no normalisation at all.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from orchestrator.services.dataset_archive import (
    DatasetArchiveError,
    summarize_dataset_archive,
)
from orchestrator.services.dataset_layout import (
    classify,
    plan_normalization,
    rewrite_archive,
)

_LIMITS = {
    "max_files": 10_000,
    "max_uncompressed_bytes": 100 * 1024 * 1024,
    "min_classes": 2,
    "max_classes": 100,
}


def _zip(tmp_path: Path, names: list[str], *, name: str = "ds.zip") -> str:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for entry in names:
            zf.writestr(entry, b"x")
    return str(path)


def _normalize(tmp_path: Path, names: list[str], *, test_fraction: float = 0.2):
    """Plan, rewrite and validate -- the whole server-side path in one call."""
    source = _zip(tmp_path, names)
    plan = plan_normalization(
        source,
        max_files=_LIMITS["max_files"],
        max_uncompressed_bytes=_LIMITS["max_uncompressed_bytes"],
        test_fraction=test_fraction,
    )
    if plan is None:
        return None, None
    destination = str(tmp_path / "normalized.zip")
    rewrite_archive(
        source,
        destination,
        plan,
        max_uncompressed_bytes=_LIMITS["max_uncompressed_bytes"],
    )
    return plan, summarize_dataset_archive(destination, **_LIMITS)


# --- The layouts that used to be rejected ------------------------------------


def test_intel_doubled_nesting_becomes_a_valid_dataset(tmp_path: Path) -> None:
    """seg_train/seg_train/<class>/ -- the shape that prompted all of this."""
    names = [
        f"seg_{split}/seg_{split}/{klass}/{index}.jpg"
        for split in ("train", "test")
        for klass in ("forest", "street")
        for index in range(3)
    ]
    names += [f"seg_pred/seg_pred/{index}.jpg" for index in range(4)]

    plan, summary = _normalize(tmp_path, names)

    assert summary is not None
    assert summary.classes == ["forest", "street"]
    assert summary.train.images == 6
    assert summary.test.images == 6
    # The unlabelled prediction folder is dropped, not folded into a split.
    assert plan.carved_images == 0
    assert any("seg_pred/" in note for note in plan.notes)


def test_flat_kaggle_filenames_become_class_directories(tmp_path: Path) -> None:
    names = [f"train/{klass}.{index}.jpg" for klass in ("cat", "dog") for index in range(5)]
    names += [f"test/{klass}.{index}.jpg" for klass in ("cat", "dog") for index in range(2)]

    _plan, summary = _normalize(tmp_path, names)

    assert summary is not None
    assert summary.classes == ["cat", "dog"]
    assert summary.train.per_class == {"cat": 5, "dog": 5}
    assert summary.test.per_class == {"cat": 2, "dog": 2}


def test_validation_folder_is_accepted_as_the_held_out_split(tmp_path: Path) -> None:
    """training/ + valid/ is a real split under another name, not an invention."""
    names = [f"training/{klass}/{index}.png" for klass in ("a", "b") for index in range(4)]
    names += [f"valid/{klass}/{index}.png" for klass in ("a", "b") for index in range(2)]

    plan, summary = _normalize(tmp_path, names)

    assert summary is not None
    assert summary.train.images == 8
    assert summary.test.images == 4
    # Nothing was invented, so nothing claims to have been.
    assert plan.carved_images == 0


def test_missing_test_split_is_carved_and_said_so(tmp_path: Path) -> None:
    names = [f"train/{klass}/{index}.png" for klass in ("a", "b") for index in range(10)]

    plan, summary = _normalize(tmp_path, names)

    assert summary is not None
    assert summary.train.images + summary.test.images == 20
    assert summary.test.images == plan.carved_images > 0
    # The judgement dataset_archive refused to make silently is made out loud.
    assert any("held out" in note for note in plan.notes)


def test_a_class_present_in_only_one_split_is_dropped_with_a_reason(
    tmp_path: Path,
) -> None:
    names = [f"train/{klass}/{index}.png" for klass in ("a", "b") for index in range(2)]
    names += [f"test/{klass}/{index}.png" for klass in ("a", "b") for index in range(2)]
    names += ["test/ghost/0.png", "test/ghost/1.png"]

    plan, summary = _normalize(tmp_path, names, test_fraction=0.0)

    assert summary is not None
    assert summary.classes == ["a", "b"]
    assert any("ghost" in note for note in plan.notes)


# --- What must not change ----------------------------------------------------


def test_a_conformant_archive_needs_no_rearranging(tmp_path: Path) -> None:
    """The pre-existing path is untouched: valid archives never reach planning.

    Asserted on the validator rather than on the plan, because that is the
    actual guarantee -- an archive that already passes is stored as uploaded.
    """
    names = ["train/cat/a.png", "train/dog/b.png", "test/cat/c.png", "test/dog/d.png"]
    summary = summarize_dataset_archive(_zip(tmp_path, names), **_LIMITS)
    assert summary.classes == ["cat", "dog"]


def test_zip_slip_is_refused_rather_than_rearranged(tmp_path: Path) -> None:
    """The whole point: a rejected archive must not be laundered into a valid one."""
    names = ["train/cat/a.png", "test/cat/b.png", "../../etc/cron.d/payload.png"]
    with pytest.raises(DatasetArchiveError, match="escapes the extraction directory"):
        plan_normalization(
            _zip(tmp_path, names),
            max_files=_LIMITS["max_files"],
            max_uncompressed_bytes=_LIMITS["max_uncompressed_bytes"],
            test_fraction=0.2,
        )


def test_symlink_entry_is_refused_rather_than_rearranged(tmp_path: Path) -> None:
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("train/cat/a.png", b"x")
        info = zipfile.ZipInfo("train/cat/link.png")
        info.external_attr = (0o120777 << 16)  # S_IFLNK
        zf.writestr(info, b"/etc/passwd")

    with pytest.raises(DatasetArchiveError, match="not a regular file"):
        plan_normalization(
            str(path),
            max_files=_LIMITS["max_files"],
            max_uncompressed_bytes=_LIMITS["max_uncompressed_bytes"],
            test_fraction=0.2,
        )


def test_entry_count_limit_still_applies(tmp_path: Path) -> None:
    names = [f"train/cat/{index}.png" for index in range(12)]
    with pytest.raises(DatasetArchiveError, match="over the 5 limit"):
        plan_normalization(
            _zip(tmp_path, names),
            max_files=5,
            max_uncompressed_bytes=_LIMITS["max_uncompressed_bytes"],
            test_fraction=0.2,
        )


def test_rewrite_meters_real_bytes_not_declared_ones(tmp_path: Path) -> None:
    """A bomb lies about its declared size, so the copy counts what it reads."""
    source = tmp_path / "bomb.zip"
    payload = b"\0" * (256 * 1024)
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as zf:
        for klass in ("a", "b"):
            for index in range(4):
                zf.writestr(f"train/{klass}/{index}.png", payload)
                zf.writestr(f"test/{klass}/{index}.png", payload)

    plan = plan_normalization(
        str(source),
        max_files=_LIMITS["max_files"],
        max_uncompressed_bytes=_LIMITS["max_uncompressed_bytes"],
        test_fraction=0.2,
    )
    assert plan is not None
    with pytest.raises(DatasetArchiveError, match="once extracted"):
        rewrite_archive(
            str(source),
            str(tmp_path / "out.zip"),
            plan,
            max_uncompressed_bytes=64 * 1024,
        )


@pytest.mark.parametrize(
    "class_dir",
    ["..%2f..%2fetc", "a/../../b", "...", "  spaced name  ", "C:", "-", "x" * 200],
)
def test_a_hostile_class_name_cannot_steer_the_target_path(class_dir: str) -> None:
    """Every target is exactly ``<split>/<one component>/<generated name>``.

    The class name is the only part of a target that comes from the uploader,
    so this is the single place a path trick could re-enter after the rewrite
    has otherwise generated everything. Rebuilt from an allowlist, it is either
    one harmless component or nothing at all -- never a separator, never a
    traversal, never a bare dot name.
    """
    _split, class_name = classify(f"train/{class_dir}/0.png")
    if class_name is None:
        return
    assert "/" not in class_name and "\\" not in class_name
    assert class_name.strip(".") != ""
    assert ":" not in class_name
    assert len(class_name) <= 64


def test_generated_targets_are_all_inside_the_two_splits(tmp_path: Path) -> None:
    names = [f"train/../{klass}/{index}.png" for klass in ("a", "b") for index in range(10)]
    plan = plan_normalization(
        _zip(tmp_path, names),
        max_files=_LIMITS["max_files"],
        max_uncompressed_bytes=_LIMITS["max_uncompressed_bytes"],
        test_fraction=0.2,
    )
    assert plan is not None
    for _member, target in plan.entries:
        parts = target.split("/")
        assert len(parts) == 3
        assert parts[0] in ("train", "test")


def test_an_unrecognisable_archive_plans_nothing(tmp_path: Path) -> None:
    """No plan means the caller reports the validator's original complaint."""
    plan = plan_normalization(
        _zip(tmp_path, ["notes.txt", "photos/1.jpg", "photos/2.jpg"]),
        max_files=_LIMITS["max_files"],
        max_uncompressed_bytes=_LIMITS["max_uncompressed_bytes"],
        test_fraction=0.2,
    )
    assert plan is None
