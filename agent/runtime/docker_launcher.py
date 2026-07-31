"""Docker Engine launch of the trainer container (ADR-007, M4; torchrun M5).

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
  * bridge networking, never ``--network host``: a single-rank job uses the
    default bridge; a multi-rank cohort (M5) uses a shared *user-defined* bridge
    network so ranks resolve each other by name for c10d rendezvous — still a
    bridge, never host networking, so the isolation profile is preserved
  * no ``--privileged``, no extra ``--cap-add``

For a multi-rank job (M5, ADR-005) the entrypoint is ``torchrun`` with real
c10d rendezvous (``--rdzv-backend=c10d`` etc. — see ``_torchrun_command``); a
single-rank job keeps the M4 image default (``python train.py``), unchanged.

``build_run_kwargs`` is pure and side-effect free specifically so the exact
flags/mounts/torchrun-argv can be unit-tested without a Docker daemon — see
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
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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
    """Node-local launch configuration (agent CLI/env — not the job spec).

    The S3/checkpoint fields (M6, ADR-006) are optional: when all of
    ``s3_endpoint_url``/``s3_access_key``/``s3_secret_key``/``s3_bucket_checkpoints``
    are set they are passed into the trainer container's env so it checkpoints to
    (and resumes from) MinIO; when unset the trainer runs without checkpointing,
    exactly as M4/M5 — and the container env is byte-for-byte the pre-M6 env.
    """

    image: str = DEFAULT_TRAINER_IMAGE
    memory_limit: str = "6g"
    pids_limit: int = 256
    dataset_cache_volume: str = DEFAULT_DATASET_CACHE_VOLUME
    s3_endpoint_url: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket_checkpoints: str | None = None
    s3_region: str = "us-east-1"
    checkpoint_every_n_steps: int = 100
    # Where the trainer caches downloaded datasets when running unsandboxed
    # (ADR-007 addendum). The container path uses the named Docker volume
    # above; a child process has no such mount, so it needs a real host path.
    unsandboxed_data_cache_dir: str = str(
        Path.home() / ".gpu-orchestrator-agent" / "data-cache"
    )

    def checkpointing_enabled(self) -> bool:
        """True iff all S3 credentials/endpoint/bucket are present."""
        return bool(
            self.s3_endpoint_url
            and self.s3_access_key
            and self.s3_secret_key
            and self.s3_bucket_checkpoints
        )


@dataclass(frozen=True)
class RendezvousSpec:
    """The cohort/rendezvous fields the orchestrator hands an agent at claim
    time (M5, ADR-005) — everything ``build_run_kwargs`` needs to launch this
    rank under ``torchrun`` with real c10d rendezvous.

    ``world_size == 1`` is the single-process path (no torchrun); the fields
    below are then unused.
    """

    world_size: int
    rank: int
    is_rendezvous_host: bool
    backend: str
    endpoint: str
    rdzv_id: str
    network: str
    host_alias: str
    max_restarts: int


@runtime_checkable
class SupportsLogs(Protocol):
    """The subset of docker-py's ``Container`` this module relies on —
    documents the real interface and lets tests substitute a lightweight
    double without importing the real SDK's model classes.

    ``runtime_checkable`` so the unsandboxed backend's ``TrainerProcess``
    (ADR-007 addendum) can assert it really satisfies this contract; the two
    execution paths share every consumer below the launch, and that only holds
    while both honour this interface."""

    def logs(self, **kwargs: Any) -> Iterator[bytes]: ...

    def wait(self) -> dict[str, Any]: ...

    def stop(self, **kwargs: Any) -> None: ...

    @property
    def id(self) -> str: ...


def _torchrun_command(rendezvous: RendezvousSpec) -> list[str]:
    """The ``torchrun`` argv that launches this rank with real c10d rendezvous
    (ADR-005). Every rank runs the same command; c10d assigns the actual torch
    rank at rendezvous, and ``--rdzv-endpoint`` points every rank at the
    rendezvous host. ``--nproc-per-node=1`` because each agent/container is one
    rank for now; ``--max-restarts`` is the bounded elastic flag (real and
    present even though M5 does not exercise deep elasticity)."""
    return [
        "torchrun",
        f"--nnodes={rendezvous.world_size}",
        "--nproc-per-node=1",
        "--rdzv-backend=c10d",
        f"--rdzv-id={rendezvous.rdzv_id}",
        f"--rdzv-endpoint={rendezvous.endpoint}",
        f"--max-restarts={rendezvous.max_restarts}",
        "train.py",
    ]


def build_run_kwargs(
    *,
    config: TrainerLaunchConfig,
    job_spec: dict[str, Any],
    job_id: str,
    lease_id: str,
    lease_epoch: int,
    has_gpu: bool,
    rendezvous: RendezvousSpec | None = None,
) -> dict[str, Any]:
    """Build the exact kwargs for ``docker_client.containers.run(**kwargs)``.

    ``job_spec`` is the job's validated spec dict (``dataset``, ``model``,
    ``epochs``, ``batch_size``, ``learning_rate``). ``has_gpu`` must reflect this
    node's own real, freshly-checked hardware (not a cached enrollment-time
    value) — GPU device requests are attached only when true, so a CPU-only peer
    node never claims a GPU it does not have.

    ``rendezvous`` (M5): when present with ``world_size > 1`` the container is
    launched under ``torchrun`` for real DDP — the entrypoint becomes the
    torchrun argv, ``DIST_BACKEND`` is set, and the container joins the cohort's
    shared user-defined network (so ranks resolve each other by name) with the
    rendezvous host taking the agreed network name/alias so its endpoint
    resolves. ``world_size == 1`` (or ``None``) keeps the M4 single-process path
    exactly: image default entrypoint, plain bridge networking.
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
    # M6 (ADR-006): pass S3/MinIO checkpoint config through to the trainer only
    # when it is fully configured, so a non-checkpointing node's container env is
    # exactly the pre-M6 env. In the dev co-located topology the endpoint is the
    # host's published MinIO port (e.g. http://host.docker.internal:9010), so the
    # trainer container reaches the same MinIO the orchestrator uses.
    if config.checkpointing_enabled():
        assert config.s3_endpoint_url is not None  # narrowed by checkpointing_enabled
        assert config.s3_access_key is not None
        assert config.s3_secret_key is not None
        assert config.s3_bucket_checkpoints is not None
        environment["S3_ENDPOINT_URL"] = config.s3_endpoint_url
        environment["S3_ACCESS_KEY"] = config.s3_access_key
        environment["S3_SECRET_KEY"] = config.s3_secret_key
        environment["S3_BUCKET_CHECKPOINTS"] = config.s3_bucket_checkpoints
        environment["S3_REGION"] = config.s3_region
        environment["CHECKPOINT_EVERY_N_STEPS"] = str(config.checkpoint_every_n_steps)
    kwargs: dict[str, Any] = {
        "image": config.image,
        "environment": environment,
        # Identifying labels so a running trainer can be traced back to the
        # lease that launched it from `docker ps` alone. Load-bearing for the
        # M9 benchmark, which induces failures by killing one specific job's
        # container (CONTRIBUTING.md rule 6: real failures, not simulated
        # flags) and must not kill a bystander to do it. Also the fastest way
        # to answer "what is this container and who asked for it" during an
        # incident.
        "labels": {
            "gpu-orchestrator.role": "trainer",
            "gpu-orchestrator.job_id": job_id,
            "gpu-orchestrator.lease_id": lease_id,
            "gpu-orchestrator.lease_epoch": str(lease_epoch),
        },
        "detach": True,
        "remove": True,  # --rm
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "read_only": True,
        "tmpfs": {"/tmp": _TMPFS_OPTS},
        "volumes": {config.dataset_cache_volume: {"bind": "/data-cache", "mode": "rw"}},
        "mem_limit": config.memory_limit,
        "pids_limit": config.pids_limit,
        "privileged": False,
        "stdout": True,
        "stderr": True,
    }

    is_ddp = rendezvous is not None and rendezvous.world_size > 1
    if is_ddp:
        assert rendezvous is not None  # narrowed by is_ddp
        environment["DIST_BACKEND"] = rendezvous.backend
        kwargs["command"] = _torchrun_command(rendezvous)
        # Cohort members share a user-defined network so c10d ranks reach each
        # other by name (ADR-007-preserving: a bridge-type network, never host
        # networking). The rendezvous host takes the agreed name so its
        # endpoint resolves for every rank.
        kwargs["network"] = rendezvous.network
        if rendezvous.is_rendezvous_host:
            kwargs["name"] = rendezvous.host_alias
    else:
        # M4 single-process path: image default entrypoint, plain bridge.
        kwargs["network_mode"] = "bridge"

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


def ensure_rendezvous_network(client: docker.DockerClient, *, name: str) -> None:
    """Create the cohort's shared user-defined bridge network if absent (M5).

    Every cohort member ensures it before launching, so whichever agent gets
    there first creates it. A concurrent "already exists" race is benign and
    ignored — the network only needs to exist, not to be created by us."""
    try:
        client.networks.get(name)
        return
    except docker.errors.NotFound:
        pass
    try:
        logger.info("creating rendezvous network %r", name)
        client.networks.create(name=name, driver="bridge", check_duplicate=True)
    except docker.errors.APIError as exc:
        # Another cohort member created it between our get and create — fine.
        logger.info("rendezvous network %r already present (%s)", name, exc)


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
            # Strip a trailing CR so a CRLF writer yields the same line a LF
            # writer does. Linux containers emit LF, so this never mattered
            # until the unsandboxed backend (ADR-007 addendum) started feeding
            # this function a Windows child process — whose CRLF left a stray
            # "\r" on every line and corrupted the trainer's machine-readable
            # metric frames.
            stripped = line.rstrip("\r")
            if stripped:
                yield stripped
    if pending:
        trailing = pending.rstrip("\r")
        if trailing:
            yield trailing


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


def stop_container(container: SupportsLogs, *, timeout: int = 10) -> bool:
    """Stop a trainer container we are abandoning. Returns True if the stop was
    issued cleanly, False if it could not be (already gone, daemon unreachable).

    Used when this agent loses the lease the container was running under (the
    job was cancelled, or the attempt was fenced out and reassigned). Leaving it
    running would keep this node occupied on work the orchestrator has already
    given to someone else, so it would sit unable to claim anything new while
    the scheduler kept offering it leases that expire unclaimed.

    Never raises: abandoning is best-effort cleanup on a path that is already
    handling a failure, and the container is launched with ``--rm``, so it may
    legitimately have exited and been removed before we get here.
    """
    try:
        container.stop(timeout=timeout)
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup, never fatal
        logger.warning(
            "could not stop abandoned container %s: %s",
            getattr(container, "id", "?"),
            exc,
        )
        return False
