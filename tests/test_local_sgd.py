"""Local SGD (SYNC_EVERY > 1) and the ResNet-18 option, on real processes.

Two real ranks over gloo train on different data under ``no_sync`` (so their
weights drift apart), then ``_average_parameters`` must leave both holding the
exact mean of what each had. Run as separate processes because that is the
only way a process group is real.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("torchvision")
mp = pytest.importorskip("torch.multiprocessing")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "trainer"))
import train  # noqa: E402


def _rank(rank: int, store: str, out: dict) -> None:  # type: ignore[type-arg]
    import torch.distributed as dist
    from torch.nn.parallel import DistributedDataParallel

    # A file store: tcp:// needs libuv, which Windows builds of torch lack.
    dist.init_process_group("gloo", init_method=store, rank=rank, world_size=2)
    torch.manual_seed(0)  # same starting weights on both ranks, as DDP ensures
    base = train.SmallCNN(in_channels=1, num_classes=3)
    model = DistributedDataParallel(base)
    optimizer = torch.optim.SGD(base.parameters(), lr=0.1)
    torch.manual_seed(100 + rank)  # different data per rank
    for _ in range(3):
        optimizer.zero_grad()
        with model.no_sync():
            torch.nn.functional.cross_entropy(
                model(torch.randn(4, 1, 8, 8)), torch.randint(0, 3, (4,))
            ).backward()
        optimizer.step()
    before = {k: v.clone() for k, v in base.state_dict().items()}
    train._average_parameters(base, 2)
    out[rank] = (before, {k: v.clone() for k, v in base.state_dict().items()})
    dist.destroy_process_group()


def test_local_sgd_ranks_end_on_the_mean_of_their_weights(tmp_path: Path) -> None:
    out = mp.Manager().dict()
    store = (tmp_path / "store").as_uri()
    mp.spawn(_rank, args=(store, out), nprocs=2, join=True)
    (before0, after0), (before1, after1) = out[0], out[1]

    weight = "conv1a.weight"
    assert not torch.equal(before0[weight], before1[weight]), "ranks never drifted"
    for key, value in after0.items():
        if not value.dtype.is_floating_point:
            continue  # num_batches_tracked: an integer counter, left alone
        assert torch.equal(value, after1[key]), f"{key} differs between ranks"
        assert torch.allclose(value, (before0[key] + before1[key]) / 2, atol=1e-6), key


def test_resnet18_small_fits_small_images() -> None:
    for channels in (1, 3):
        model = train.resnet18_small(channels, 10)
        assert model(torch.randn(2, channels, 32, 32)).shape == (2, 10)
    assert sum(p.numel() for p in model.parameters()) > 11_000_000
