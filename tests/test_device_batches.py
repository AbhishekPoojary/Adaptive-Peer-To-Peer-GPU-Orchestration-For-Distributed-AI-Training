"""Device-resident batching must be the DataLoader pipeline it replaced, only faster.

``DeviceBatches`` took GPU utilization from ~53% to ~85% on CIFAR-10 at batch
256 (bench/report, gpu_utilization). That is only worth having if the batches
are the same ones: same normalisation as torchvision's transforms, the same
augmentation family, and -- the one that would hang a run rather than skew it
-- every DDP rank getting the same number of steps.

Runs on CPU tensors so it needs no GPU. Skipped where torch is not installed
(the CI image installs only the control plane's dependencies).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("torchvision")

# train.py imports its siblings flat, as it does inside the trainer image.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "trainer"))
import train  # noqa: E402


def _batches(n: int, **kwargs: object) -> train.DeviceBatches:
    images = torch.arange(n, dtype=torch.uint8).view(n, 1, 1, 1).expand(n, 1, 4, 4).contiguous()
    labels = torch.arange(n, dtype=torch.int64)
    defaults: dict[str, object] = {
        "batch_size": 3,
        "mean": (0.0,),
        "std": (1.0,),
        "augment": False,
        "shuffle": True,
    }
    defaults.update(kwargs)
    return train.DeviceBatches(images, labels, **defaults)  # type: ignore[arg-type]


def _labels(batches: train.DeviceBatches) -> list[int]:
    return [int(v) for _x, y in batches for v in y]


def test_one_epoch_visits_every_example_once() -> None:
    seen = _labels(_batches(10))
    assert sorted(seen) == list(range(10))


def test_ddp_ranks_get_equal_step_counts_and_cover_everything() -> None:
    """10 examples over 3 ranks: DistributedSampler pads to 12, so each rank
    takes 4 and runs the same number of steps. Unequal counts would leave one
    rank waiting forever in the gradient all-reduce."""
    ranks = [_batches(10, rank=r, world_size=3) for r in range(3)]
    per_rank = [_labels(b) for b in ranks]
    assert [len(p) for p in per_rank] == [4, 4, 4]
    assert len({len(b) for b in ranks}) == 1
    assert set().union(*map(set, per_rank)) == set(range(10))


def test_shuffle_is_seeded_by_epoch() -> None:
    a, b = _batches(50), _batches(50)
    a.set_epoch(3)
    b.set_epoch(3)
    assert _labels(a) == _labels(b)
    b.set_epoch(4)
    assert _labels(a) != _labels(b)


def test_normalisation_matches_torchvision() -> None:
    from torchvision import transforms

    image = torch.randint(0, 256, (3, 8, 8), dtype=torch.uint8)
    mean, std = (0.4914, 0.4822, 0.4465), (0.2470, 0.2435, 0.2616)
    expected = transforms.Normalize(mean, std)(image.float() / 255.0)
    batches = train.DeviceBatches(
        image.unsqueeze(0), torch.tensor([0]), batch_size=1, mean=mean, std=std,
        augment=False, shuffle=False,
    )
    (got, _), = list(batches)
    torch.testing.assert_close(got[0], expected)


def test_augmentation_is_a_padded_crop_possibly_flipped() -> None:
    """Every augmented image must equal some 32x32 window of the zero-padded
    original, either way round -- exactly RandomCrop(32, padding=4) followed by
    RandomHorizontalFlip."""
    x = torch.rand(6, 3, 32, 32)
    generator = torch.Generator().manual_seed(0)
    out = train._random_crop_flip(x, padding=4, generator=generator)
    padded = torch.nn.functional.pad(x, (4, 4, 4, 4))
    for i in range(6):
        windows = [
            padded[i, :, dy : dy + 32, dx : dx + 32] for dy in range(9) for dx in range(9)
        ]
        assert any(
            torch.equal(out[i], w) or torch.equal(out[i], w.flip(2)) for w in windows
        ), i
