"""The benchmark harness must refuse to publish anything it did not measure.

CONTRIBUTING.md rule 6 says every benchmark number is machine-written to a
timestamped artifact with a git SHA and hardware inventory. These tests pin the
half of that rule which is easy to lose: that the harness fails *loudly* rather
than emitting a plausible-looking artifact with a hole in it. A report with a
null where a metric belongs is worse than no report, because a later reader
will average over it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from bench.harness.artifact import (
    IncompleteMeasurementError,
    build_artifact,
    load_schema,
    write_artifact,
)
from bench.harness.inventory import Limitations, assess_limitations, capture_hardware

_GOOD_LIMITATIONS = Limitations(
    load_term_differentiable=False,
    latency_term_differentiable=False,
    notes=["single host"],
).as_dict()


def _artifact(results: dict[str, Any]) -> dict[str, Any]:
    return build_artifact(
        scenario="unit_test",
        git_sha="0123456789abcdef0123456789abcdef01234567",
        hardware={"cpu": "test", "gpu": None, "ram_gb": 16.0, "os": "test"},
        results=results,
        limitations=_GOOD_LIMITATIONS,
    )


# --- Refusing to fabricate ----------------------------------------------------


def test_a_null_result_is_refused(tmp_path: Path) -> None:
    """The core guarantee. A metric the harness could not measure must stop the
    artifact from existing, not be published as an unknown."""
    artifact = _artifact({"recovery_seconds": None})
    with pytest.raises(IncompleteMeasurementError, match="recovery_seconds"):
        write_artifact(artifact, report_dir=tmp_path)
    assert list(tmp_path.iterdir()) == [], "nothing may be left behind"


def test_a_null_nested_deep_in_results_is_still_refused(tmp_path: Path) -> None:
    """Shallow checking would let a hole through inside a list of trials —
    which is exactly where a real scenario's per-run measurements live."""
    artifact = _artifact(
        {"trials": [{"seconds": 1.5}, {"seconds": None}], "total": 2}
    )
    with pytest.raises(IncompleteMeasurementError, match=r"trials\[1\]\.seconds"):
        write_artifact(artifact, report_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_an_artifact_missing_its_limitations_block_is_refused(tmp_path: Path) -> None:
    """ADR-013: every report must declare which score terms it did not
    exercise, so a reader taking only the JSON cannot mistake a partial
    validation for a complete one."""
    artifact = _artifact({"ok": 1})
    del artifact["limitations"]
    with pytest.raises(jsonschema.ValidationError):
        write_artifact(artifact, report_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_hand_typed_git_sha_is_refused(tmp_path: Path) -> None:
    """The schema's SHA pattern is what stops "unknown" or a branch name from
    standing in for the commit that was actually measured."""
    artifact = _artifact({"ok": 1})
    artifact["git_sha"] = "not-a-sha"
    with pytest.raises(jsonschema.ValidationError):
        write_artifact(artifact, report_dir=tmp_path)


# --- Writing a real artifact --------------------------------------------------


def test_a_complete_artifact_validates_and_is_written(tmp_path: Path) -> None:
    artifact = _artifact({"recovery_seconds": 12.5, "final_test_accuracy": 0.98})
    path = write_artifact(artifact, report_dir=tmp_path)

    assert path.exists()
    written = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validate(written, load_schema())
    assert written["results"]["recovery_seconds"] == 12.5
    assert written["scenario"] == "unit_test"
    assert path.name.endswith("-unit_test.json")


def test_zero_is_a_measurement_and_is_allowed(tmp_path: Path) -> None:
    """A measured zero is real data — 0 placements on the degraded node is the
    result we hope for. Only *absent* measurements are refused."""
    artifact = _artifact({"placed_on_degraded": 0, "healthy_share": 0.0})
    assert write_artifact(artifact, report_dir=tmp_path).exists()


def test_provisional_runs_are_marked(tmp_path: Path) -> None:
    """--allow-dirty must leave a trace in the artifact, or a reader would
    believe the git_sha describes the code that ran."""
    artifact = build_artifact(
        scenario="unit_test",
        git_sha="0123456789abcdef0123456789abcdef01234567",
        hardware={"cpu": "test", "gpu": None, "ram_gb": 16.0, "os": "test"},
        results={"ok": 1},
        limitations=_GOOD_LIMITATIONS,
        provisional=True,
    )
    written = json.loads(
        write_artifact(artifact, report_dir=tmp_path).read_text(encoding="utf-8")
    )
    assert written["provisional"] is True
    assert "dirty worktree" in written["provisional_reason"]


# --- Honest self-assessment of what a run could measure -----------------------


def test_single_host_fleet_declares_load_and_latency_untested() -> None:
    """Two agents reporting the same hostname are the same machine, so the
    alpha*L and gamma*D terms cannot have been differentiated (ADR-013)."""
    limits = assess_limitations(["my-laptop", "my-laptop"])
    assert limits.load_term_differentiable is False
    assert limits.latency_term_differentiable is False
    assert "one physical host" in limits.notes[0]


def test_multi_host_fleet_declares_them_tested() -> None:
    """The same code must report honestly in the other direction — this is what
    makes the scenario valid unchanged once real peer laptops join."""
    limits = assess_limitations(["laptop-a", "laptop-b", "laptop-c"])
    assert limits.load_term_differentiable is True
    assert limits.latency_term_differentiable is True
    assert "3 distinct physical hosts" in limits.notes[0]


def test_hardware_capture_reports_real_values_or_null() -> None:
    """Rule 2: absent hardware is null, never a plausible substitute."""
    hardware = capture_hardware()
    assert hardware.ram_gb > 0
    assert hardware.cpu
    assert hardware.os
    # gpu is None on a CPU-only machine — a truthful answer, not a failure.
    assert hardware.gpu is None or isinstance(hardware.gpu, str)


def test_shipped_scenario_configs_are_loadable_and_complete() -> None:
    """A scenario config missing a key would fail minutes into a real run."""
    from bench.harness.__main__ import SCENARIOS, load_scenario

    for name in SCENARIOS:
        config = load_scenario(name)
        assert "job_spec" in config, f"{name} has no job_spec"
        spec = config["job_spec"]
        for key in (
            "dataset",
            "model",
            "epochs",
            "batch_size",
            "learning_rate",
            "world_size",
        ):
            assert key in spec, f"{name}.job_spec is missing {key}"
