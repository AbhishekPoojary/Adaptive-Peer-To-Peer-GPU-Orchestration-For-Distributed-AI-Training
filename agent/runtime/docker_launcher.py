"""Docker Engine launch of the trainer container (ADR-007, M4).

Exact isolation profile — fixed by ADR-007, do not deviate:
  * ``--rm``                         (auto-cleanup)
  * ``--gpus all``                   (only when this node's own hardware has a
                                       GPU — see ``has_gpu``; omitted entirely
                                       for a true CPU node)
  * ``--cap-drop=ALL``
  * ``--security-opt=no-new-privileges``
  * ``--read-only`` root filesystem
  * ``--tmpfs /tmp:rw,noexec,nosuid,size=2g``
  * a named dataset-cache volume mounted at ``/data-cache`` (created once per
    node if absent), so downloads are reused across runs
  * ``--memory`` / ``--pids-limit`` from node config
  * bridge networking (never ``--network host``)
  * no ``--privileged``, no extra ``--cap-add``

``build_run_kwargs`` is pure and side-effect free specifically so the exact
flags/mounts can be unit-tested without a Docker daemon — see
``tests/test_docker_launcher.py``'s ``FakeDockerClient``. Everything else here
is a thin wrapper around the real ``docker`` SDK and is exercised by the
end-to-end verification run, not a unit test.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

import docker
import docker.errors
import docker.types

logger = logging.getLogger("agent.runtime.docker_launcher")

#: Created once per node if absent; dataset downloads are reused across runs.
DEFAULT_DATASET_CACHE_VOLUME = "gpu-orchestrator-dataset-cache"
DEFAULT_TRAINER_IMAGE = "gpu-orchestrator-trainer:latest"
_TMPFS_OPTS = "rw,noexec,nosuid,size=2g"


@dataclass(frozen=True)
class TrainerLaunchConfig:
    """Node-local launch configuration (agent CLI/env — not the job spec)."""

    image: str = DEFAULT_TRAINER_IMAGE
    memory_limit: str = "6g"
    pids_limit: int = 256
    dataset_cache_volume: str = DEFAULT_DATASET_CACHE_VOLUME


class SupportsLogs(Protocol):
    """The subset of docker-py's ``Container`` this module relies on —
    documents the real interface and lets tests substitute a lightweight
    double without importing the real SDK's model classes."""

    def logs(self, **kwargs: Any) -> Iterator[bytes]: ...

    def wait(self) -> dict[str, Any]: ...

    @property
    def id(self) -> str: ...


def build_run_kwargs(
    *,
    config: TrainerLaunchConfig,
    job_spec: dict[str, Any],
    job_id: str,
    lease_id: str,
    lease_epoch: int,
    has_gpu: bool,
) -> dict[str, Any]:
    """Build the exact kwargs for ``docker_client.containers.run(**kwargs)``.

    ``job_spec`` is the job's validated spec dict (``dataset``, ``model``,
    ``epochs``, ``batch_size``, ``learning_rate`` — the existing M2 shape;
    nothing new is invented here). ``has_gpu`` must reflect this node's own
    real, freshly-checked hardware (not a cached enrollment-time value) — GPU
    device requests are attached only when true, so a CPU-only peer node never
    claims a GPU it does not have.
    """
    environment = {
        "DATASET": str(job_spec["dataset"]),
        "MODEL": str(job_spec["model"]),
        "EPOCHS": str(job_spec["epochs"]),
        "BATCH_SIZE": str(job_spec["batch_size"]),
        "LEARNING_RATE": str(job_spec["learning_rate"]),
        "JOB_ID": job_id,
        "LEASE_ID": lease_id,
        "LEASE_EPOCH": str(lease_epoch),
    }
    kwargs: dict[str, Any] = {
        "image": config.image,
        "environment": environment,
        "detach": True,
        "remove": True,  # --rm
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "read_only": True,
        "tmpfs": {"/tmp": _TMPFS_OPTS},
        "volumes": {config.dataset_cache_volume: {"bind": "/data-cache", "mode": "rw"}},
        "mem_limit": config.memory_limit,
        "pids_limit": config.pids_limit,
        "network_mode": "bridge",
        "privileged": False,
        "stdout": True,
        "stderr": True,
    }
    if has_gpu:
        kwargs["device_requests"] = [
            docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
        ]
    return kwargs


def ensure_dataset_cache_volume(client: docker.DockerClient, *, name: str) -> None:
    """Create the node's dataset-cache volume if it doesn't already exist."""
    try:
        client.volumes.get(name)
    except docker.errors.NotFound:
        logger.info("creating dataset cache volume %r", name)
        client.volumes.create(name=name)


def launch_trainer_container(client: docker.DockerClient, *, run_kwargs: dict[str, Any]) -> Any:
    """Start the trainer container for real. Raises on any Docker-level launch
    failure (missing image, GPU request rejected, daemon unreachable, ...) —
    the caller must fail the lease honestly rather than assume success."""
    return client.containers.run(**run_kwargs)


#: Sentinel placed on the internal merge queue when one stream's pump thread
#: finishes (used only inside stream_container_logs).
_STREAM_DONE = object()


def _iter_single_stream_lines(
    container: SupportsLogs, *, want_stdout: bool, want_stderr: bool
) -> Iterator[str]:
    """Yield complete lines from one already-demultiplexed docker-py log
    stream (exactly one of stdout/stderr requested).

    docker-py's ``Container.logs()`` demultiplexes Docker's non-tty container
    log protocol *internally* before returning anything to the caller
    (``APIClient._get_result_tty`` -> ``_multiplexed_response_stream_helper``)
    — there is no 8-byte frame header left in the bytes it yields. But when
    both ``stdout`` and ``stderr`` are requested together, that internal
    helper merges them into one undifferentiated byte stream with the
    stdout/stderr distinction discarded. Requesting exactly one of the two at
    a time (this function) keeps that distinction, at the cost of needing a
    separate log-follow connection per stream (see ``stream_container_logs``).

    Lines are buffered across chunks so a line split across multiple HTTP
    chunks is never forwarded as a truncated fragment — this matters because
    the trainer's machine-readable metric/final JSON lines must arrive intact
    for ``agent.runtime.metrics`` to parse them.
    """
    raw_chunks = container.logs(stream=True, follow=True, stdout=want_stdout, stderr=want_stderr)
    pending = ""
    for chunk in raw_chunks:
        buffered = pending + chunk.decode("utf-8", errors="replace")
        *complete_lines, pending = buffered.split("\n")
        for line in complete_lines:
            if line:
                yield line
    if pending:
        yield pending


def stream_container_logs(container: SupportsLogs) -> Iterator[tuple[str, str]]:
    """Yield real ``(stream, line)`` pairs as the container's stdout/stderr
    arrives in real time, each correctly tagged "stdout"/"stderr".

    Runs two internal worker threads — one following stdout only, one
    following stderr only (see ``_iter_single_stream_lines``) — merged into a
    single output queue in arrival order, since a single ``logs()`` call
    covering both streams together loses which-is-which (see that function's
    docstring).

    Blocking/synchronous overall (docker-py has no async API) — callers on an
    asyncio event loop run this whole function in its own worker thread (see
    ``agent.runtime.execution``); the two threads started here are an
    internal implementation detail.
    """
    merged: queue.Queue[tuple[str, str] | object] = queue.Queue()

    def _pump(stream_name: str, want_stdout: bool, want_stderr: bool) -> None:
        try:
            for line in _iter_single_stream_lines(
                container, want_stdout=want_stdout, want_stderr=want_stderr
            ):
                merged.put((stream_name, line))
        except Exception as exc:  # noqa: BLE001 - report, never crash silently
            logger.error("log pump for %s failed: %s", stream_name, exc)
        finally:
            merged.put(_STREAM_DONE)

    threads = [
        threading.Thread(target=_pump, args=("stdout", True, False), daemon=True),
        threading.Thread(target=_pump, args=("stderr", False, True), daemon=True),
    ]
    for t in threads:
        t.start()

    remaining = len(threads)
    while remaining > 0:
        item = merged.get()
        if item is _STREAM_DONE:
            remaining -= 1
            continue
        yield item  # type: ignore[misc]

    for t in threads:
        t.join(timeout=5.0)


def wait_for_exit(container: SupportsLogs) -> int:
    """Block until the container exits; return its real exit code."""
    result = container.wait()
    return int(result.get("StatusCode", 1))
