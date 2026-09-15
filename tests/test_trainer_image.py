"""The trainer image must contain everything train.py imports.

This exists because it did not, once, and the way it failed is the reason the
check is worth having. ``train.py`` gained ``from dataset_spec import ...``; the
Dockerfile copied ``train.py`` and ``checkpoint.py`` and nothing else. Nothing
broke at build time, nothing broke in the test suite, and nothing broke on the
machine that made the change. It broke at the first line of every training run,
on a volunteer's hardware, as::

    ModuleNotFoundError: No module named 'dataset_spec'

and surfaced to the operator as "trainer exited with code 1" two failed
attempts later.

The container runs ``python train.py`` rather than importing a package, so a
flat ``import x`` resolves against ``/app`` and nowhere else. That makes the
COPY list a real interface between two files that nothing else connects, and
the sort of interface someone editing one of them will not think to check.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_TRAINER = Path(__file__).resolve().parent.parent / "trainer"


def _flat_imports(source: Path) -> set[str]:
    """Modules ``train.py`` imports by bare name, i.e. expects beside itself.

    Anything importable from the environment -- torch, json, os -- is excluded
    by checking whether a file of that name sits in ``trainer/``. What is left
    is exactly the set that has to be copied into the image.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return {name for name in names if (_TRAINER / f"{name}.py").exists()}


def _copied_modules(dockerfile: Path) -> set[str]:
    """Module names the Dockerfile places next to train.py in /app."""
    copied: set[str] = set()
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*COPY\s+trainer/(\S+\.py)\s+\./(\S+\.py)\s*$", line)
        if match:
            copied.add(Path(match.group(2)).stem)
    return copied


def test_dockerfile_copies_every_module_train_imports() -> None:
    train = _TRAINER / "train.py"
    dockerfile = _TRAINER / "Dockerfile"

    needed = _flat_imports(train)
    copied = _copied_modules(dockerfile)

    assert needed, "parsed no local imports from train.py — the parser is wrong"
    missing = needed - copied
    assert not missing, (
        f"trainer/Dockerfile does not copy {sorted(missing)}, which train.py "
        "imports. The image would build fine and every training run would die "
        "with ModuleNotFoundError on a volunteer's machine."
    )


def test_copied_modules_are_import_light() -> None:
    """Modules beside train.py must not drag heavy deps into the orchestrator.

    ``checkpoint`` and ``dataset_spec`` are both imported by the orchestrator as
    ``trainer.*``. That works only while they stay free of torch, which is not
    installed in the orchestrator image -- and the failure mode is the
    orchestrator refusing to start, which is worse than the bug this file was
    written for.
    """
    train = _TRAINER / "train.py"
    for name in _flat_imports(train):
        source = (_TRAINER / f"{name}.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        heavy = imported & {"torch", "torchvision", "numpy"}
        assert not heavy, f"trainer/{name}.py imports {sorted(heavy)}"
