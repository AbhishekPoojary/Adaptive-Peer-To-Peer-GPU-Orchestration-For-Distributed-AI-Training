"""Check a dataset zip against the orchestrator's own validator, offline.

Same code path the upload endpoint runs, so a PASS here means the upload will
be accepted -- no need to burn a login session finding out.

Usage:  python check_dataset.py <archive.zip>
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from orchestrator.services.dataset_archive import (  # noqa: E402
    DatasetArchiveError,
    summarize_dataset_archive,
)

# The orchestrator's defaults (orchestrator/core/config.py).
LIMITS = dict(
    max_files=200_000,
    max_uncompressed_bytes=8 * 1024**3,
    min_classes=2,
    max_classes=1000,
)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    path = argv[0]
    try:
        s = summarize_dataset_archive(path, **LIMITS)
    except DatasetArchiveError as exc:
        print(f"REJECTED: {exc}")
        return 1

    print("ACCEPTED -- this archive will upload.")
    print(f"  classes ({s.num_classes}): {', '.join(s.classes)}")
    for split, summary in (("train", s.train), ("test", s.test)):
        detail = ", ".join(f"{c}={n}" for c, n in sorted(summary.per_class.items()))
        print(f"  {split}: {summary.images} images ({detail})")
    print(f"  uncompressed: {s.total_uncompressed_bytes / 1024**2:.1f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
