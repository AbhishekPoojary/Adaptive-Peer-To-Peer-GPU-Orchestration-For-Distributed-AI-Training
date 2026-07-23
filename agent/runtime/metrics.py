"""Pure parsing helpers for the trainer's machine-readable stdout contract
(M4).

``trainer/train.py`` prints exactly one JSON line per real training event:

    per epoch:  {"type": "metric", "epoch": N, "loss": F, "test_accuracy": F}
    on success: {"type": "final", "epochs_completed": N, "final_loss": F,
                 "final_test_accuracy": F, "device": "cuda"|"cpu"}

These functions recognize *only* lines matching that exact shape (right
keys, right types). Anything else — progress bars, torch/cuda info, dataset
download logs, a stray line that happens to be valid JSON but isn't this
shape — returns ``None`` and is forwarded as an ordinary log line, never
coerced into a fabricated metric. Pure and side-effect free: no Docker
daemon, network, or GPU required to exercise this logic (see
``tests/test_agent_metrics_parsing.py``).
"""

from __future__ import annotations

import json
from typing import Any, TypeGuard


def _is_real_number(value: object) -> TypeGuard[float | int]:
    """True for int/float, explicitly excluding bool (a `bool` is an `int`
    subclass in Python, but ``True``/``False`` are never a valid loss/epoch)."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def parse_metric_line(line: str) -> dict[str, Any] | None:
    """Return ``{"epoch", "loss", "test_accuracy"}`` iff ``line`` is exactly a
    metric frame; otherwise ``None``."""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("type") != "metric":
        return None

    epoch = data.get("epoch")
    loss = data.get("loss")
    test_accuracy = data.get("test_accuracy")

    if not isinstance(epoch, int) or isinstance(epoch, bool):
        return None
    if not _is_real_number(loss):
        return None
    if test_accuracy is not None and not _is_real_number(test_accuracy):
        return None

    return {
        "epoch": epoch,
        "loss": float(loss),
        "test_accuracy": float(test_accuracy) if test_accuracy is not None else None,
    }


def parse_final_line(line: str) -> dict[str, Any] | None:
    """Return the final-summary dict iff ``line`` is exactly a final frame;
    otherwise ``None``."""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("type") != "final":
        return None

    epochs_completed = data.get("epochs_completed")
    final_loss = data.get("final_loss")
    final_test_accuracy = data.get("final_test_accuracy")
    device = data.get("device")

    if not isinstance(epochs_completed, int) or isinstance(epochs_completed, bool):
        return None
    if not _is_real_number(final_loss):
        return None
    if not _is_real_number(final_test_accuracy):
        return None
    if device not in ("cuda", "cpu"):
        return None

    return {
        "epochs_completed": epochs_completed,
        "final_loss": float(final_loss),
        "final_test_accuracy": float(final_test_accuracy),
        "device": device,
    }
