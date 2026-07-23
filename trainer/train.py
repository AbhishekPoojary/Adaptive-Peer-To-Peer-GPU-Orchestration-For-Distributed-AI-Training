"""Real training entrypoint for the trainer container (M4).

Driven entirely by environment variables — no hidden config:

    DATASET        "cifar10" | "mnist"
    MODEL          free text; only "small_cnn" (a compact, dataset-agnostic
                   CNN — see SmallCNN below) is implemented in M4. Any other
                   value is honestly logged as unrecognized and SmallCNN is
                   used anyway, rather than silently pretending to run
                   whatever architecture name was requested.
    EPOCHS         positive int
    BATCH_SIZE     positive int
    LEARNING_RATE  positive float
    JOB_ID         opaque string, forwarded into logs only
    LEASE_ID       opaque string, forwarded into logs only
    LEASE_EPOCH    opaque string, forwarded into logs only
    TORCH_DATA_CACHE  dataset cache directory (default /data-cache — a mounted
                      volume, not baked into the image, so repeated runs on
                      the same node reuse the download)
    NUM_WORKERS    DataLoader worker count (default 2)

Every dataset is the real torchvision CIFAR-10/MNIST download, split exactly
as torchvision ships it (the canonical test set is the held-out set — never
shuffled into train). Every forward/backward/optimizer step is real; there is
no synthetic data and no sleep-based stand-in for compute anywhere in this
file (CONTRIBUTING.md rule 4). Trains on GPU when available, otherwise CPU
with an explicit, honest log line — GPU use is never claimed when it did not
happen.

Machine-readable contract (stdout, one exact JSON line per event — nothing
else on stdout may match this shape):
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
from typing import Literal

import torch
import torch.nn as nn
import torch.nn.functional as F  # noqa: N812 - idiomatic PyTorch import alias
from torch.utils.data import DataLoader
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

    if model_name.lower() not in ("small_cnn", "cnn"):
        _log(
            f"MODEL='{model_name}' is not implemented; only 'small_cnn' exists in M4 — "
            f"using SmallCNN honestly rather than fabricating another architecture."
        )

    _log(
        f"job_id={job_id} lease_id={lease_id} lease_epoch={lease_epoch} "
        f"dataset={dataset_name} model={model_name} epochs={epochs} "
        f"batch_size={batch_size} learning_rate={learning_rate}"
    )
    _log(f"torch={torch.__version__} cuda_build={torch.version.cuda}")

    if torch.cuda.is_available():
        device = torch.device("cuda")
        _log(f"CUDA available: training on GPU ({torch.cuda.get_device_name(device)})")
    else:
        device = torch.device("cpu")
        _log("CUDA not available: training on CPU (honest fallback, no GPU is being used)")

    _log(f"loading real dataset '{dataset_name}' into cache dir '{cache_dir}' (torchvision)...")
    t0 = time.monotonic()
    train_set, test_set, in_channels, num_classes = _build_datasets(dataset_name, cache_dir)
    _log(
        f"dataset ready in {time.monotonic() - t0:.1f}s: "
        f"{len(train_set)} train / {len(test_set)} test examples (canonical split)"
    )

    pin_memory = device.type == "cuda"
    train_loader = DataLoader(
        train_set,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers,
        pin_memory=pin_memory,
        drop_last=False,
    )
    test_loader = DataLoader(
        test_set,
        batch_size=max(batch_size, 256),
        shuffle=False,
        num_workers=num_workers,
        pin_memory=pin_memory,
    )

    model = SmallCNN(in_channels=in_channels, num_classes=num_classes).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    criterion = nn.CrossEntropyLoss()

    final_loss = float("nan")
    final_test_accuracy = 0.0

    for epoch in range(1, epochs + 1):
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
            loss.backward()
            optimizer.step()

            running_loss += loss.item()
            n_batches += 1

        mean_loss = running_loss / max(n_batches, 1)

        model.eval()
        correct = 0
        total = 0
        with torch.no_grad():
            for images, labels in test_loader:
                images = images.to(device, non_blocking=pin_memory)
                labels = labels.to(device, non_blocking=pin_memory)
                outputs = model(images)
                predictions = outputs.argmax(dim=1)
                correct += int((predictions == labels).sum().item())
                total += labels.size(0)
        test_accuracy = correct / total if total > 0 else 0.0

        _log(
            f"epoch {epoch}/{epochs} done in {time.monotonic() - epoch_t0:.1f}s: "
            f"train_loss={mean_loss:.4f} test_accuracy={test_accuracy:.4f} "
            f"({correct}/{total})"
        )
        _emit_metric(epoch=epoch, loss=mean_loss, test_accuracy=test_accuracy)

        final_loss = mean_loss
        final_test_accuracy = test_accuracy

    _emit_final(
        epochs_completed=epochs,
        final_loss=final_loss,
        final_test_accuracy=final_test_accuracy,
        device=device.type,
    )
    _log("training complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - top-level guard: log clearly, never fake success
        print(f"[train] FATAL: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    sys.exit(0)
