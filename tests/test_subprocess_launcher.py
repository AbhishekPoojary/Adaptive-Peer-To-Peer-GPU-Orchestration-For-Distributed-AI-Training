"""The unsandboxed execution backend (ADR-007 addendum).

Docker stays the default. This path exists so a peer with only Python — no
Docker, no NVIDIA Container Toolkit, no WSL2 — can still contribute a GPU,
because those three installs are where most volunteers give up.

The design claim under test is that this is a second *backend*, not a second
subsystem: ``TrainerProcess`` satisfies the same ``SupportsLogs`` protocol the
container path defines, so log streaming, metric parsing, cancellation and
abandonment all work against it unchanged. If that stops being true, the two
paths will drift and only one will be tested.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from agent.runtime.docker_launcher import (
    SupportsLogs,
    TrainerLaunchConfig,
    build_run_kwargs,
    stream_container_logs,
    wait_for_exit,
)
from agent.runtime.subprocess_launcher import (
    TrainerSourceNotFoundError,
    find_trainer_script,
    launch_trainer_process,
    parse_memory_limit,
)

_SPEC = {
    "dataset": "mnist",
    "model": "cnn",
    "epochs": 1,
    "batch_size": 8,
    "learning_rate": 0.1,
    "world_size": 1,
    "min_gpu_mem_bytes": None,
}


def _fake_trainer(tmp_path: Path, body: str) -> Path:
    """Write a stand-in trainer that exercises the real streaming contract."""
    script = tmp_path / "train.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def _launch(script: Path, tmp_path: Path, **kwargs: object) -> SupportsLogs:
    return launch_trainer_process(
        environment=kwargs.get("environment", {}),  # type: ignore[arg-type]
        data_cache_dir=tmp_path / "cache",
        memory_limit_bytes=kwargs.get("memory_limit_bytes"),  # type: ignore[arg-type]
        trainer_script=script,
    )


# --- It satisfies the container path's contract -------------------------------


def test_it_satisfies_the_supportslogs_protocol(tmp_path: Path) -> None:
    """The whole design rests on this. If it stops holding, execution.py's
    streaming/cancellation pipeline silently applies to only one backend."""
    script = _fake_trainer(tmp_path, "print('hello')")
    process = _launch(script, tmp_path)
    try:
        assert isinstance(process, SupportsLogs)
    finally:
        process.wait()


def test_stream_container_logs_works_against_it_unchanged(tmp_path: Path) -> None:
    """The real docker-path streamer, driven by a subprocess."""
    script = _fake_trainer(
        tmp_path,
        """
        import sys
        print("to stdout", flush=True)
        print("to stderr", file=sys.stderr, flush=True)
        """,
    )
    process = _launch(script, tmp_path)
    lines = list(stream_container_logs(process))
    assert wait_for_exit(process) == 0

    by_stream = {stream: line for stream, line in lines}
    assert by_stream["stdout"] == "to stdout"
    assert by_stream["stderr"] == "to stderr"


def test_a_real_exit_code_is_reported(tmp_path: Path) -> None:
    """A failing trainer must surface its real code, so fail_lease can record
    it — exit 137 vs exit 1 is what M11's retry reasoning turns on."""
    script = _fake_trainer(tmp_path, "import sys; sys.exit(3)")
    process = _launch(script, tmp_path)
    assert wait_for_exit(process) == 3


def test_metric_lines_survive_intact(tmp_path: Path) -> None:
    """The trainer's stdout is a machine-readable protocol. A buffered pipe
    would hold these back until exit, turning live progress into a final dump —
    which is why the launcher forces PYTHONUNBUFFERED."""
    from agent.runtime.metrics import parse_metric_line

    script = _fake_trainer(
        tmp_path,
        """
        import json
        print(json.dumps({
            "type": "metric", "epoch": 1, "loss": 0.5,
            "test_accuracy": 0.9, "step": 10,
        }), flush=True)
        """,
    )
    process = _launch(script, tmp_path)
    lines = [line for stream, line in stream_container_logs(process) if stream == "stdout"]
    process.wait()

    parsed = [parse_metric_line(line) for line in lines]
    assert any(p is not None and p["epoch"] == 1 for p in parsed)


# --- Configuration comes from one source of truth -----------------------------


def test_the_trainer_env_matches_the_container_path(tmp_path: Path) -> None:
    """Both backends must configure the trainer identically, or a job would
    behave differently depending on which peer took it. The subprocess path
    reuses build_run_kwargs' environment dict rather than rebuilding it."""
    kwargs = build_run_kwargs(
        config=TrainerLaunchConfig(),
        job_spec=dict(_SPEC),
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=2,
        has_gpu=False,
    )
    environment = kwargs["environment"]

    script = _fake_trainer(
        tmp_path,
        """
        import json, os
        print(json.dumps({
            k: os.environ.get(k)
            for k in ("DATASET", "MODEL", "EPOCHS", "BATCH_SIZE",
                      "LEARNING_RATE", "JOB_ID", "LEASE_ID", "LEASE_EPOCH",
                      "TORCH_DATA_CACHE")
        }), flush=True)
        """,
    )
    process = _launch(script, tmp_path, environment=environment)
    out = [line for stream, line in stream_container_logs(process) if stream == "stdout"]
    process.wait()

    import json

    seen = json.loads(out[0])
    assert seen["DATASET"] == "mnist"
    assert seen["EPOCHS"] == "1"
    assert seen["JOB_ID"] == "job-1"
    assert seen["LEASE_EPOCH"] == "2"
    # The one deliberate difference: the container's /data-cache mount does not
    # exist here, so it points at a real host directory.
    assert seen["TORCH_DATA_CACHE"] == str(tmp_path / "cache")


# --- Failing loudly rather than silently --------------------------------------


def test_a_missing_trainer_script_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer without train.py must fail its lease honestly, not appear to run
    and report nothing."""
    monkeypatch.delenv("TRAINER_SCRIPT", raising=False)
    monkeypatch.setattr(
        "agent.runtime.subprocess_launcher._PACKAGE_ROOT", tmp_path / "nowhere"
    )
    with pytest.raises(TrainerSourceNotFoundError, match="train.py"):
        find_trainer_script()


def test_an_explicit_script_path_wins(tmp_path: Path) -> None:
    script = _fake_trainer(tmp_path, "pass")
    assert find_trainer_script(str(script)) == script


# --- The memory guard (a mitigation, not cgroups) -----------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("6g", 6 * 1024**3), ("512m", 512 * 1024**2), ("1024k", 1024 * 1024),
     ("4096", 4096), ("0", None), ("none", None), ("", None)],
)
def test_memory_limits_parse_like_docker(text: str, expected: int | None) -> None:
    """Same format as --trainer-memory-limit on the container path, so one
    value means the same thing on both."""
    assert parse_memory_limit(text) == expected


@pytest.mark.skipif(sys.platform == "darwin", reason="RSS accounting differs on macOS")
def test_a_runaway_allocation_is_killed(tmp_path: Path) -> None:
    """The guard's whole reason to exist: without it, a runaway job on a
    volunteer's laptop has no limit at all.

    Sampled, so this asserts the process is killed — not that it is killed
    before exceeding the limit, which polling cannot guarantee and which the
    ADR does not claim.
    """
    script = _fake_trainer(
        tmp_path,
        """
        import time
        blocks = []
        for _ in range(400):
            blocks.append(bytearray(8 * 1024 * 1024))   # 8 MB a time
            time.sleep(0.02)
        print("SHOULD NOT FINISH", flush=True)
        """,
    )
    process = _launch(script, tmp_path, memory_limit_bytes=96 * 1024**2)
    exit_code = wait_for_exit(process)

    assert exit_code != 0, "a killed process must not report success"
    assert process.killed_for_memory is True, (  # type: ignore[attr-defined]
        "the guard must report why, so the peer sees a truthful reason rather "
        "than an uninterpretable exit code"
    )


def test_no_limit_means_no_guard(tmp_path: Path) -> None:
    """memory_limit_bytes=None must not start a watcher thread that kills
    anything — a peer who disabled the limit gets no limit, not a silent one."""
    script = _fake_trainer(tmp_path, "print('fine', flush=True)")
    process = _launch(script, tmp_path, memory_limit_bytes=None)
    assert wait_for_exit(process) == 0
    assert process.killed_for_memory is False  # type: ignore[attr-defined]
