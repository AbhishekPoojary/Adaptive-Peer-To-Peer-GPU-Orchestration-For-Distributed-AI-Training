"""Uploaded datasets are decoded once and then served without the CPU decoding
anything (trainer/train.py, "decode once, train many times").

The cache is only worth having if it is the same data: the same pixels the old
DataLoader transform produced, the same labels, every example once per epoch,
and equal shard lengths under DDP. Measured in the trainer image on a real
14k-image upload, the decoded pixels matched the DataLoader's exactly; this
pins that on a small synthetic upload so a change to either side is caught.

Runs on CPU. Skipped where torch is not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("torchvision")
Image = pytest.importorskip("PIL.Image")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "trainer"))
import train  # noqa: E402


def _upload(root: Path) -> None:
    """A tiny ImageFolder upload with awkward inputs: mixed sizes, a greyscale
    image and a palette image, as real uploads contain."""
    colours = {"cat": (200, 30, 30), "dog": (30, 30, 200)}
    for split, count in (("train", 5), ("test", 2)):
        for klass, rgb in colours.items():
            folder = root / split / klass
            folder.mkdir(parents=True)
            for i in range(count):
                image = Image.new("RGB", (40 + 13 * i, 70 - 5 * i), rgb)
                image.putpixel((i, i), (255, 255, 255))
                if i == 1:
                    image = image.convert("L")
                if i == 2:
                    image = image.convert("P")
                image.save(folder / f"{i}.png")


@pytest.fixture
def decoded(tmp_path: Path) -> tuple[Path, object, object, dict[str, object]]:
    _upload(tmp_path)
    train_set, test_set, _channels, _classes = train._build_custom_datasets(
        str(tmp_path), expected_num_classes=2
    )
    return tmp_path, train_set, test_set, train._decoded_custom_dataset(
        str(tmp_path), train_set, test_set
    )


def test_decoded_pixels_and_labels_match_the_dataloader(decoded) -> None:  # type: ignore[no-untyped-def]
    _root, train_set, _test_set, cache = decoded
    images, labels = cache["train"]
    assert images.shape == (10, 3, train._CUSTOM_IMAGE_SIZE, train._CUSTOM_IMAGE_SIZE)
    for i in range(len(train_set)):
        expected, expected_label = train_set[i]
        got = (torch.from_numpy(images[i].copy()).float() / 255.0 - 0.5) / 0.5
        torch.testing.assert_close(got, expected, atol=1e-6, rtol=0)
        assert int(labels[i]) == expected_label


def test_second_job_reuses_the_cache(decoded) -> None:  # type: ignore[no-untyped-def]
    root, train_set, test_set, _cache = decoded
    marker = next(root.glob(".decoded-*px")) / "train.images.npy"
    before = marker.stat().st_mtime_ns
    train._decoded_custom_dataset(str(root), train_set, test_set)
    assert marker.stat().st_mtime_ns == before
    assert not list(root.glob(".decoding-*")), "no staging directory may be left behind"


def test_streamed_batches_cover_every_example_with_equal_ddp_shards(decoded) -> None:  # type: ignore[no-untyped-def]
    _root, _train_set, _test_set, cache = decoded
    images, labels = cache["train"]
    shards = [
        train.HostBatches(
            images, labels, device=torch.device("cpu"), batch_size=3,
            mean=train._CUSTOM_MEAN, std=train._CUSTOM_STD, shuffle=True,
            rank=rank, world_size=3,
        )
        for rank in range(3)
    ]
    seen = [[int(v) for _x, y in shard for v in y] for shard in shards]
    assert [len(s) for s in seen] == [4, 4, 4]  # 10 padded to 12, as DistributedSampler
    assert sorted(set().union(*map(set, seen))) == sorted(set(int(v) for v in labels))
    x, _y = next(iter(shards[0]))
    assert x.dtype == torch.float32 and x.min() >= -1.0 and x.max() <= 1.0
