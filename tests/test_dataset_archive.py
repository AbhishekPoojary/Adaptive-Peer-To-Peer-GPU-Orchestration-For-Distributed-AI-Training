"""Dataset archive validation (ADR-014).

These build real zip files — including deliberately malicious ones — and assert
the validator refuses them. The attacks here are not hypothetical: zip slip and
symlink entries are the two standard ways an archive turns "extract this" into
"write anywhere", and this archive gets extracted on a volunteer's laptop.

No image bytes are needed: the validator reads only the zip central directory
and never decompresses, so an entry's *name* and declared size are the whole
input. That is deliberate — a validator that decompressed to inspect would be
vulnerable to the very bombs it is meant to catch.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from orchestrator.services.dataset_archive import (
    DatasetArchiveError,
    summarize_dataset_archive,
)

_LIMITS = {
    "max_files": 10_000,
    "max_uncompressed_bytes": 100 * 1024 * 1024,
    "min_classes": 2,
    "max_classes": 100,
}


def _zip(tmp_path: Path, names: list[str], *, name: str = "ds.zip") -> str:
    """Write a zip containing ``names`` with tiny placeholder contents."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for entry in names:
            zf.writestr(entry, b"x")
    return str(path)


def _valid_names() -> list[str]:
    return [
        "train/cat/a.png",
        "train/cat/b.png",
        "train/dog/c.png",
        "test/cat/d.png",
        "test/dog/e.png",
    ]


def summarize(path: str):  # type: ignore[no-untyped-def]
    return summarize_dataset_archive(path, **_LIMITS)


# --- The happy path ----------------------------------------------------------


def test_accepts_an_imagefolder_archive(tmp_path: Path) -> None:
    summary = summarize(_zip(tmp_path, _valid_names()))

    assert summary.classes == ["cat", "dog"]
    assert summary.num_classes == 2
    assert summary.train.images == 3
    assert summary.test.images == 2
    assert summary.total_images == 5
    assert summary.train.per_class == {"cat": 2, "dog": 1}


def test_tolerates_a_single_wrapping_directory(tmp_path: Path) -> None:
    """Zipping a folder rather than its contents is the commonest near-miss."""
    wrapped = [f"my-dataset/{n}" for n in _valid_names()]
    summary = summarize(_zip(tmp_path, wrapped))
    assert summary.classes == ["cat", "dog"]
    assert summary.total_images == 5


def test_ignores_macos_metadata(tmp_path: Path) -> None:
    """A zip made in Finder carries __MACOSX/ and .DS_Store; neither is an error."""
    names = [
        *_valid_names(),
        "__MACOSX/._a.png",
        "train/cat/.DS_Store",
        ".DS_Store",
    ]
    summary = summarize(_zip(tmp_path, names))
    assert summary.total_images == 5


def test_accepts_mixed_image_types(tmp_path: Path) -> None:
    names = [
        "train/cat/a.PNG",
        "train/dog/b.jpeg",
        "test/cat/c.JPG",
        "test/dog/d.bmp",
    ]
    summary = summarize(_zip(tmp_path, names))
    assert summary.total_images == 4


# --- Path traversal and symlinks ---------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc/cron.d/pwned",
        "train/cat/../../../../etc/passwd",
        "/etc/passwd",
        "/absolute/train/cat/a.png",
    ],
)
def test_refuses_path_traversal(tmp_path: Path, hostile: str) -> None:
    """Zip slip: an entry that writes outside the extraction directory.

    zipfile.writestr stores the name verbatim, so these archives are exactly
    what an attacker would hand us.
    """
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for entry in _valid_names():
            zf.writestr(entry, b"x")
        zf.writestr(hostile, b"owned")

    with pytest.raises(DatasetArchiveError) as exc:
        summarize(str(path))
    assert "escapes" in str(exc.value) or "absolute" in str(exc.value)


def test_refuses_a_symlink_entry(tmp_path: Path) -> None:
    """A symlink entry turns a later write into a write *through* the link."""
    path = tmp_path / "symlink.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for entry in _valid_names():
            zf.writestr(entry, b"x")
        # 0xA1FF0000 = S_IFLNK | 0777 in the high 16 bits, which is how a real
        # archiver records a symlink.
        info = zipfile.ZipInfo("train/cat/link.png")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")

    with pytest.raises(DatasetArchiveError, match="not a regular file"):
        summarize(str(path))


def test_refuses_a_drive_qualified_path(tmp_path: Path) -> None:
    path = tmp_path / "drive.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for entry in _valid_names():
            zf.writestr(entry, b"x")
        zf.writestr("C:windows/system32/evil.png", b"x")

    with pytest.raises(DatasetArchiveError, match="drive-qualified"):
        summarize(str(path))


# --- Smuggled executables and bombs ------------------------------------------


def test_refuses_a_non_image_file(tmp_path: Path) -> None:
    """A .py beside the images is only inert until something globs the dir."""
    names = [*_valid_names(), "train/cat/backdoor.py"]
    with pytest.raises(DatasetArchiveError, match="not an image"):
        summarize(_zip(tmp_path, names))


def test_refuses_a_declared_size_bomb(tmp_path: Path) -> None:
    """Kilobytes on the wire, terabytes on disk.

    Caught from the *declared* sizes in the central directory, before anything
    is decompressed — which is the only point at which it is still cheap.
    """
    path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for entry in _valid_names():
            zf.writestr(entry, b"x")
        # 200 MiB of zeros compresses to almost nothing but blows the 100 MiB cap.
        zf.writestr("train/cat/huge.png", b"\0" * (200 * 1024 * 1024))

    with pytest.raises(DatasetArchiveError, match="expands to more than"):
        summarize(str(path))


def test_refuses_too_many_entries(tmp_path: Path) -> None:
    names = [f"train/cat/{i}.png" for i in range(60)]
    names += [f"test/cat/{i}.png" for i in range(60)]
    names += ["train/dog/a.png", "test/dog/b.png"]
    with pytest.raises(DatasetArchiveError, match="over the"):
        summarize_dataset_archive(
            _zip(tmp_path, names),
            max_files=50,
            max_uncompressed_bytes=_LIMITS["max_uncompressed_bytes"],
            min_classes=2,
            max_classes=100,
        )


def test_refuses_a_file_that_is_not_a_zip(tmp_path: Path) -> None:
    path = tmp_path / "notazip.zip"
    path.write_bytes(b"I am a JPEG, honest")
    with pytest.raises(DatasetArchiveError, match="not a readable .zip"):
        summarize(str(path))


# --- Layout mistakes that would corrupt the reported accuracy ----------------


def test_requires_a_test_split(tmp_path: Path) -> None:
    """Held-out accuracy has to be measured on data the submitter held out.

    Auto-splitting train/ would make every reported number depend on a decision
    the reader cannot see.
    """
    names = ["train/cat/a.png", "train/dog/b.png"]
    with pytest.raises(DatasetArchiveError, match="no test/ split"):
        summarize(_zip(tmp_path, names))


def test_requires_a_train_split(tmp_path: Path) -> None:
    names = ["test/cat/a.png", "test/dog/b.png"]
    with pytest.raises(DatasetArchiveError, match="no train/ split"):
        summarize(_zip(tmp_path, names))


def test_refuses_mismatched_classes(tmp_path: Path) -> None:
    """Scoring on a label the model was never taught is a meaningless number."""
    names = [
        "train/cat/a.png",
        "train/dog/b.png",
        "test/cat/c.png",
        "test/dog/d.png",
        "test/emu/e.png",
    ]
    with pytest.raises(DatasetArchiveError, match="different classes"):
        summarize(_zip(tmp_path, names))
    # ...and it says which side the stray class was on.
    with pytest.raises(DatasetArchiveError, match="only in test/: emu"):
        summarize(_zip(tmp_path, names, name="ds2.zip"))


def test_refuses_a_single_class(tmp_path: Path) -> None:
    names = ["train/cat/a.png", "test/cat/b.png"]
    with pytest.raises(DatasetArchiveError, match="at least 2 classes"):
        summarize(_zip(tmp_path, names))


def test_error_names_what_it_found_instead(tmp_path: Path) -> None:
    """A wrong layout must say what was there, not just that it was wrong."""
    names = ["images/cat/a.png", "images/dog/b.png"]
    with pytest.raises(DatasetArchiveError) as exc:
        summarize(_zip(tmp_path, names))
    message = str(exc.value)
    assert "train/<class-name>" in message
    assert "images" in message
