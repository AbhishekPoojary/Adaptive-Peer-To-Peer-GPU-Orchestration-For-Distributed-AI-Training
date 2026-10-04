"""The trainer's gradient-sync measurement (trainer/train.py: _profile_sync).

Runs a real two-process DDP group over gloo on CPU -- two genuine processes and
a genuine all-reduce, not a mock -- and checks that the measurement is sane and
that it leaves the model exactly as it found it, BatchNorm statistics included.
Skipped where torch is not installed.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

_TRAINER = Path(__file__).resolve().parent.parent / "trainer"


def _rank(rank: int, init_file: str, results: object) -> None:
    import torch.distributed as dist

    sys.path.insert(0, str(_TRAINER))
    import train

    dist.init_process_group("gloo", init_method=f"file://{init_file}", rank=rank, world_size=2)
    torch.manual_seed(0)
    base = train.SmallCNN(in_channels=1, num_classes=10)
    before = {k: v.clone() for k, v in base.state_dict().items()}
    model = torch.nn.parallel.DistributedDataParallel(base)
    batches = [(torch.randn(16, 1, 28, 28), torch.randint(0, 10, (16,))) for _ in range(4)]
    profile = train._profile_sync(model, base, batches, torch.nn.CrossEntropyLoss(),
                                  torch.device("cpu"))
    after = base.state_dict()
    restored = all(torch.equal(before[k], after[k]) for k in before)
    grads_clear = all(p.grad is None for p in base.parameters())
    results[rank] = (profile, restored, grads_clear)  # type: ignore[index]
    dist.destroy_process_group()


def test_sync_profile_measures_and_restores() -> None:
    import torch.multiprocessing as mp

    with tempfile.TemporaryDirectory() as tmp:
        init_file = str(Path(tmp) / "rendezvous").replace("\\", "/")
        manager = mp.Manager()
        results = manager.dict()
        mp.spawn(_rank, args=(init_file, results), nprocs=2, join=True)
        for rank in (0, 1):
            profile, restored, grads_clear = results[rank]
            assert profile["step_seconds_with_sync"] > 0
            assert profile["step_seconds_without_sync"] > 0
            assert 0.0 <= profile["sync_share_of_step"] < 1.0
            assert restored, f"rank {rank}: the measurement changed the model"
            assert grads_clear, f"rank {rank}: gradients were left behind"
