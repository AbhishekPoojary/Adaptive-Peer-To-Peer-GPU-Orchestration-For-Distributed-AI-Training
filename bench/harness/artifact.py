"""Benchmark artifact writing (CONTRIBUTING.md rule 6).

Every number this project publishes is machine-written here, into a
timestamped JSON file carrying the git SHA and the real hardware inventory it
was measured on. Nothing is hand-typed, and nothing is written that the
harness did not actually measure.

The central rule, enforced by :func:`write_artifact`: a run that could not
complete its measurements writes **no file at all**. A missing artifact is an
honest "this did not run". A partial one — nulls where a metric should be,
zeros standing in for "didn't happen" — is worse than nothing, because a later
reader will average over it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

_REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = _REPO_ROOT / "bench" / "schema.json"
REPORT_DIR = _REPO_ROOT / "bench" / "report"


class IncompleteMeasurementError(Exception):
    """A scenario could not measure something it promised to measure.

    Raised by scenarios, and deliberately fatal: the harness exits non-zero and
    writes nothing rather than publishing a report with a hole in it.
    """


def load_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema


def _reject_nulls(node: Any, path: str = "results") -> None:
    """Recursively refuse a null anywhere in the measured results.

    A ``null`` in *telemetry* is meaningful and required (rule 2: "no GPU
    reading" must not become 0.0). A ``null`` in a *benchmark result* is
    different — it means the harness did not measure the thing it said it
    would, and the artifact should not exist. Telemetry that legitimately has
    no value is recorded as a count of how many samples lacked it, not as a
    null the reader has to interpret.
    """
    if node is None:
        raise IncompleteMeasurementError(
            f"{path} is null — a benchmark result must be measured or the run "
            f"must fail; it may not be published as an unknown."
        )
    if isinstance(node, dict):
        for key, value in node.items():
            _reject_nulls(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _reject_nulls(value, f"{path}[{index}]")


def build_artifact(
    *,
    scenario: str,
    git_sha: str,
    hardware: dict[str, Any],
    results: dict[str, Any],
    limitations: dict[str, Any],
    provisional: bool = False,
) -> dict[str, Any]:
    """Assemble a schema-conforming artifact. Does not write it."""
    artifact: dict[str, Any] = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "git_sha": git_sha,
        "hardware": hardware,
        "scenario": scenario,
        "results": results,
        # ADR-013: what this run did NOT exercise, machine-written next to the
        # numbers so it cannot be quoted around.
        "limitations": limitations,
    }
    if provisional:
        # Set only when --allow-dirty was used, so a reader can tell that the
        # git_sha does not fully describe the code that ran.
        artifact["provisional"] = True
        artifact["provisional_reason"] = (
            "run from a dirty worktree; git_sha does not describe the exact "
            "code that produced these numbers"
        )
    return artifact


def write_artifact(artifact: dict[str, Any], *, report_dir: Path | None = None) -> Path:
    """Validate and write the artifact. Returns the path written.

    Validation happens *before* the file exists, so a rejected artifact leaves
    nothing behind for someone to find later and mistake for a real result.
    """
    _reject_nulls(artifact["results"])
    jsonschema.validate(artifact, load_schema())

    target_dir = report_dir or REPORT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = (
        artifact["timestamp_utc"]
        .replace(":", "")
        .replace("-", "")
        .replace("+0000", "Z")
        .split(".")[0]
    )
    path = target_dir / f"{stamp}-{artifact['scenario']}.json"
    path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    return path
