"""Real training entrypoint for the trainer container (M4 single-process, M5 DDP).

Driven entirely by environment variables — no hidden config:

    DATASET        "cifar10" | "mnist"
    MODEL          free text; only "small_cnn" (a compact, dataset-agnostic
                   CNN — see SmallCNN below) is implemented. Any other value is
                   honestly logged as unrecognized and SmallCNN is used anyway,
                   rather than silently pretending to run whatever architecture
                   name was requested.
    EPOCHS         positive int
    BATCH_SIZE     positive int  (per-rank batch size under DDP)
    LEARNING_RATE  positive float
    JOB_ID         opaque string, forwarded into logs only
    LEASE_ID       opaque string, forwarded into logs only
    LEASE_EPOCH    opaque string, forwarded into logs only
    TORCH_DATA_CACHE  dataset cache directory (default /data-cache — a mounted
                      volume, not baked into the image, so repeated runs on
                      the same node reuse the download)
    NUM_WORKERS    DataLoader worker count (default 2)
    DIST_BACKEND   torch.distributed backend when run under torchrun: "gloo"
                   (default, M5's verified backend on shared/mixed hardware) or
                   "nccl" (intended once real distinct multi-GPU hardware
                   exists — ADR-005). Ignored in the single-process path.

Distributed (DDP, M5, ADR-005): when launched under ``torchrun`` the launcher
sets ``WORLD_SIZE`` > 1 (plus ``RANK``/``LOCAL_RANK``); this file then joins the
process group, wraps the model in ``DistributedDataParallel``, shards the
training set with a ``DistributedSampler`` (each rank trains on a distinct
slice), and lets DDP all-reduce gradients across ranks every step — so N real
processes train one model together. Only rank 0 reports metrics/final lines to
avoid duplicate/conflicting rows; every rank genuinely participates in the
gradient sync (a silent rank>0 would break DDP's collective, not merely omit
logs). ``WORLD_SIZE`` unset or 1 is the M4 single-process path, preserved
exactly.

Every dataset is the real torchvision CIFAR-10/MNIST download, split exactly
as torchvision ships it (the canonical test set is the held-out set — never
shuffled into train). Every forward/backward/optimizer step is real; there is
no synthetic data and no sleep-based stand-in for compute anywhere in this
file (CONTRIBUTING.md rule 4). Trains on GPU when available (single-process, or
DDP with the nccl backend), otherwise CPU with an explicit, honest log line —
GPU use is never claimed when it did not happen.

Machine-readable contract (rank-0 stdout, one exact JSON line per event —
nothing else on stdout may match this shape):
    per epoch:  {"type": "metric", "epoch": N, "loss": F, "test_accuracy": F}
    on success: {"type": "final", "epochs_completed": N, "final_loss": F,
                 "final_test_accuracy": F, "device": "cuda"|"cpu"}
All other output (progress, torch/cuda info, download progress) is ordinary
human-readable log lines to stdout/stderr; the agent forwards everything but
only lines parsing as the exact metric/final JSON shape are treated specially.

On any unhandled exception this exits non-zero with a clear stderr message —
never a fake success.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Literal

import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F  # noqa: N812 - idiomatic PyTorch import alias
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data import DataLoader, DistributedSampler
from torchvision import datasets, transforms

# --- Dataset-specific real normalization stats (standard, published values,
# not invented) and channel/class shapes. -------------------------------------

_CIFAR10_MEAN = (0.4914, 0.4822, 0.4465)
_CIFAR10_STD = (0.2470, 0.2435, 0.2616)
_MNIST_MEAN = (0.1307,)
_MNIST_STD = (0.3081,)


class SmallCNN(nn.Module):
    """A compact, dataset-agnostic CNN sized for CIFAR-10/MNIST on a 4GB card.

    Deliberately not a full ImageNet-scale ResNet18 at 224px — that is the
    wrong size and wasteful for 28x28/32x32 inputs on consumer hardware.
    Four conv blocks (BatchNorm + ReLU) with two downsampling max-pools, then
    global average pooling so the same module handles both CIFAR-10's 32x32x3
    and MNIST's 28x28x1 inputs without shape-specific code.
    """

    def __init__(self, in_channels: int, num_classes: int) -> None:
        super().__init__()
        self.conv1a = nn.Conv2d(in_channels, 32, 3, padding=1)
        self.bn1a = nn.BatchNorm2d(32)
        self.conv1b = nn.Conv2d(32, 32, 3, padding=1)
        self.bn1b = nn.BatchNorm2d(32)
        self.conv2a = nn.Conv2d(32, 64, 3, padding=1)
        self.bn2a = nn.BatchNorm2d(64)
        self.conv2b = nn.Conv2d(64, 64, 3, padding=1)
        self.bn2b = nn.BatchNorm2d(64)
        self.conv3 = nn.Conv2d(64, 128, 3, padding=1)
        self.bn3 = nn.BatchNorm2d(128)
        self.pool = nn.MaxPool2d(2)
        self.global_pool = nn.AdaptiveAvgPool2d(1)
        self.classifier = nn.Linear(128, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.relu(self.bn1a(self.conv1a(x)))
        x = F.relu(self.bn1b(self.conv1b(x)))
        x = self.pool(x)
        x = F.relu(self.bn2a(self.conv2a(x)))
        x = F.relu(self.bn2b(self.conv2b(x)))
        x = self.pool(x)
        x = F.relu(self.bn3(self.conv3(x)))
        x = self.global_pool(x)
        x = torch.flatten(x, 1)
        return self.classifier(x)


def _log(message: str) -> None:
    """Human-readable progress line to stdout (never the machine metric shape)."""
    print(f"[train] {message}", flush=True)


def _emit_metric(*, epoch: int, loss: float, test_accuracy: float) -> None:
    print(
        json.dumps(
            {"type": "metric", "epoch": epoch, "loss": loss, "test_accuracy": test_accuracy}
        ),
        flush=True,
    )


def _emit_final(
    *, epochs_completed: int, final_loss: float, final_test_accuracy: float, device: str
) -> None:
    print(
        json.dumps(
            {
                "type": "final",
                "epochs_completed": epochs_completed,
                "final_loss": final_loss,
                "final_test_accuracy": final_test_accuracy,
                "device": device,
            }
        ),
        flush=True,
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


@dataclass(frozen=True)
class DistConfig:
    """Distributed run configuration read from the torchrun-set environment.

    ``distributed`` is False for the M4 single-process path (``WORLD_SIZE`` unset
    or 1). When True, ``rank``/``world_size``/``local_rank`` come from torchrun
    and ``backend`` from ``DIST_BACKEND`` (default gloo).
    """

    distributed: bool
    rank: int
    world_size: int
    local_rank: int
    backend: str

    @property
    def is_main(self) -> bool:
        """Only rank 0 reports metrics/final lines (avoids duplicate rows)."""
        return self.rank == 0


def _read_dist_config() -> DistConfig:
    """Read the distributed topology torchrun exports into the environment.

    ``WORLD_SIZE`` > 1 means this process is one rank of a torchrun-launched
    cohort; anything else is the single-process path."""
    world_size = int(os.environ.get("WORLD_SIZE", "1"))
    if world_size <= 1:
        return DistConfig(
            distributed=False, rank=0, world_size=1, local_rank=0, backend="none"
        )
    return DistConfig(
        distributed=True,
        rank=int(os.environ.get("RANK", "0")),
        world_size=world_size,
        local_rank=int(os.environ.get("LOCAL_RANK", "0")),
        backend=os.environ.get("DIST_BACKEND", "gloo").lower(),
    )


def _select_device(dist_config: DistConfig) -> torch.device:
    """Pick the real compute device, honestly.

    Single-process: GPU when available, else CPU (M4 behaviour, unchanged).
    DDP with nccl: this rank's own GPU (cuda:local_rank). DDP with gloo: CPU —
    gloo is a CPU collectives backend, and M5's verification runs it on the
    shared-single-GPU / mixed-hardware dev box where two real GPU processes
    cannot coexist (ADR-010). GPU use is never claimed when it did not happen.
    """
    if not dist_config.distributed:
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if dist_config.backend == "nccl" and torch.cuda.is_available():
        return torch.device(f"cuda:{dist_config.local_rank}")
    return torch.device("cpu")


def _build_datasets(
    dataset_name: Literal["cifar10", "mnist"], cache_dir: str
) -> tuple[torch.utils.data.Dataset, torch.utils.data.Dataset, int, int]:
    """Return (train_set, test_set, in_channels, num_classes) for a real,
    downloaded torchvision dataset, using its canonical train/test split."""
    if dataset_name == "cifar10":
        train_transform = transforms.Compose(
            [
                transforms.RandomCrop(32, padding=4),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                transforms.Normalize(_CIFAR10_MEAN, _CIFAR10_STD),
            ]
        )
        test_transform = transforms.Compose(
            [transforms.ToTensor(), transforms.Normalize(_CIFAR10_MEAN, _CIFAR10_STD)]
        )
        train_set = datasets.CIFAR10(
            root=cache_dir, train=True, download=True, transform=train_transform
        )
        test_set = datasets.CIFAR10(
            root=cache_dir, train=False, download=True, transform=test_transform
        )
        return train_set, test_set, 3, 10

    if dataset_name == "mnist":
        train_transform = transforms.Compose(
            [transforms.ToTensor(), transforms.Normalize(_MNIST_MEAN, _MNIST_STD)]
        )
        test_transform = train_transform
        train_set = datasets.MNIST(
            root=cache_dir, train=True, download=True, transform=train_transform
        )
        test_set = datasets.MNIST(
            root=cache_dir, train=False, download=True, transform=test_transform
        )
        return train_set, test_set, 1, 10

    raise RuntimeError(f"unsupported DATASET '{dataset_name}'")


def _epoch_mean_loss(
    running_loss: float, n_batches: int, dist_config: DistConfig, device: torch.device
) -> float:
    """The epoch's mean training loss.

    Single-process: this process's own mean. DDP: the true cross-rank mean —
    ``running_loss`` and ``n_batches`` are all-reduced (SUM) so rank 0 reports a
    loss computed over the whole dataset (every rank's shard), directly
    comparable to the single-process baseline. The all-reduce is a real
    collective every rank joins, so it also confirms the process group is live.
    """
    if not dist_config.distributed:
        return running_loss / max(n_batches, 1)
    totals = torch.tensor([running_loss, float(n_batches)], device=device)
    dist.all_reduce(totals, op=dist.ReduceOp.SUM)
    summed_loss, summed_batches = totals[0].item(), totals[1].item()
    return summed_loss / max(summed_batches, 1.0)


def main() -> None:
    dataset_name = _require_env("DATASET").lower()
    if dataset_name not in ("cifar10", "mnist"):
        raise RuntimeError(f"unsupported DATASET '{dataset_name}'; expected cifar10 or mnist")

    model_name = os.environ.get("MODEL", "small_cnn")
    epochs = int(_require_env("EPOCHS"))
    batch_size = int(_require_env("BATCH_SIZE"))
    learning_rate = float(_require_env("LEARNING_RATE"))
    job_id = os.environ.get("JOB_ID", "unknown")
    lease_id = os.environ.get("LEASE_ID", "unknown")
    lease_epoch = os.environ.get("LEASE_EPOCH", "unknown")
    cache_dir = os.environ.get("TORCH_DATA_CACHE", "/data-cache")
    num_workers = int(os.environ.get("NUM_WORKERS", "2"))

    if epochs < 1:
        raise RuntimeError(f"EPOCHS must be >= 1, got {epochs}")
    if batch_size < 1:
        raise RuntimeError(f"BATCH_SIZE must be >= 1, got {batch_size}")
    if learning_rate <= 0:
        raise RuntimeError(f"LEARNING_RATE must be > 0, got {learning_rate}")

    dist_config = _read_dist_config()
    device = _select_device(dist_config)

    def log(message: str) -> None:
        """Human-readable log line, rank-tagged under DDP. Never the metric shape."""
        if dist_config.distributed:
            _log(f"[rank {dist_config.rank}/{dist_config.world_size}] {message}")
        else:
            _log(message)

    if model_name.lower() not in ("small_cnn", "cnn"):
        log(
            f"MODEL='{model_name}' is not implemented; only 'small_cnn' exists — "
            f"using SmallCNN honestly rather than fabricating another architecture."
        )

    log(
        f"job_id={job_id} lease_id={lease_id} lease_epoch={lease_epoch} "
        f"dataset={dataset_name} model={model_name} epochs={epochs} "
        f"batch_size={batch_size}(per-rank) learning_rate={learning_rate}"
    )
    log(f"torch={torch.__version__} cuda_build={torch.version.cuda}")

    if dist_config.distributed:
        # env:// init: torchrun's c10d rendezvous already exported RANK/WORLD_SIZE
        # and the rendezvous endpoint; init_process_group forms the group.
        log(
            f"DDP: joining process group backend={dist_config.backend} "
            f"world_size={dist_config.world_size} rank={dist_config.rank} "
            f"local_rank={dist_config.local_rank}"
        )
        dist.init_process_group(backend=dist_config.backend)
        log("DDP: process group established (rendezvous complete)")

    if device.type == "cuda":
        log(f"training on GPU ({torch.cuda.get_device_name(device)})")
    else:
        reason = (
            "DDP gloo backend is CPU collectives"
            if dist_config.distributed
            else "CUDA not available"
        )
        log(f"training on CPU ({reason}; no GPU is being used) — honest")

    log(f"loading real dataset '{dataset_name}' into cache dir '{cache_dir}' (torchvision)...")
    t0 = time.monotonic()
    train_set, test_set, in_channels, num_classes = _build_datasets(dataset_name, cache_dir)
    log(
        f"dataset ready in {time.monotonic() - t0:.1f}s: "
        f"{len(train_set)} train / {len(test_set)} test examples (canonical split)"
    )

    pin_memory = device.type == "cuda"
    # DDP: a DistributedSampler shards the training set so each rank trains on a
    # distinct, non-overlapping slice — the data-parallel half of DDP.
    train_sampler: DistributedSampler | None = (
        DistributedSampler(
            train_set,
            num_replicas=dist_config.world_size,
            rank=dist_config.rank,
            shuffle=True,
            drop_last=False,
        )
        if dist_config.distributed
        else None
    )
    train_loader = DataLoader(
        train_set,
        batch_size=batch_size,
        shuffle=train_sampler is None,
        sampler=train_sampler,
        num_workers=num_workers,
        pin_memory=pin_memory,
        drop_last=False,
    )
    # Only rank 0 evaluates the held-out set (single, unsharded pass) and reports.
    test_loader = DataLoader(
        test_set,
        batch_size=max(batch_size, 256),
        shuffle=False,
        num_workers=num_workers,
        pin_memory=pin_memory,
    )

    base_model = SmallCNN(in_channels=in_channels, num_classes=num_classes).to(device)
    model: nn.Module
    if dist_config.distributed:
        # DDP all-reduces gradients across ranks every backward — the model-
        # parallel-sync half. device_ids only for a real per-rank GPU (nccl).
        device_ids = [dist_config.local_rank] if device.type == "cuda" else None
        model = DistributedDataParallel(base_model, device_ids=device_ids)
    else:
        model = base_model
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    criterion = nn.CrossEntropyLoss()

    final_loss = float("nan")
    final_test_accuracy = 0.0

    for epoch in range(1, epochs + 1):
        if train_sampler is not None:
            train_sampler.set_epoch(epoch)  # reshuffle shards deterministically per epoch
        model.train()
        running_loss = 0.0
        n_batches = 0
        epoch_t0 = time.monotonic()
        for images, labels in train_loader:
            images = images.to(device, non_blocking=pin_memory)
            labels = labels.to(device, non_blocking=pin_memory)

            optimizer.zero_grad()
            outputs = model(images)
            loss = criterion(outputs, labels)
            loss.backward()  # DDP hooks all-reduce gradients here across ranks
            optimizer.step()

            running_loss += loss.item()
            n_batches += 1

        mean_loss = _epoch_mean_loss(running_loss, n_batches, dist_config, device)

        # Only rank 0 evaluates + reports (the model is identical on every rank
        # after gradient sync, so rank 0's eval is the cohort's true accuracy).
        if dist_config.is_main:
            eval_model = model.module if isinstance(model, DistributedDataParallel) else model
            eval_model.eval()
            correct = 0
            total = 0
            with torch.no_grad():
                for images, labels in test_loader:
                    images = images.to(device, non_blocking=pin_memory)
                    labels = labels.to(device, non_blocking=pin_memory)
                    outputs = eval_model(images)
                    predictions = outputs.argmax(dim=1)
                    correct += int((predictions == labels).sum().item())
                    total += labels.size(0)
            test_accuracy = correct / total if total > 0 else 0.0

            log(
                f"epoch {epoch}/{epochs} done in {time.monotonic() - epoch_t0:.1f}s: "
                f"train_loss={mean_loss:.4f} test_accuracy={test_accuracy:.4f} "
                f"({correct}/{total})"
            )
            _emit_metric(epoch=epoch, loss=mean_loss, test_accuracy=test_accuracy)
            final_loss = mean_loss
            final_test_accuracy = test_accuracy
        else:
            log(
                f"epoch {epoch}/{epochs} done in {time.monotonic() - epoch_t0:.1f}s: "
                f"train_loss={mean_loss:.4f} (rank>0 trains silently, no eval/report)"
            )

    if dist_config.distributed:
        dist.barrier()  # every rank reaches the end before the group is destroyed

    if dist_config.is_main:
        _emit_final(
            epochs_completed=epochs,
            final_loss=final_loss,
            final_test_accuracy=final_test_accuracy,
            device=device.type,
        )
    log("training complete")

    if dist_config.distributed:
        dist.destroy_process_group()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - top-level guard: log clearly, never fake success
        print(f"[train] FATAL: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    sys.exit(0)
