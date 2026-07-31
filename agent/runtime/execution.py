"""Lease execution orchestration (M4): launch the real trainer container for
a claimed lease, stream its output over the WebSocket log/metric channel as it
runs, and report the real outcome.

This is what replaces the M2 stub ("lease held; execution arrives in M4",
never runs anything). Docker's log tail and ``wait()`` calls are blocking
(docker-py has no async API), so both run in dedicated worker threads while
this coroutine forwards lines to the orchestrator and watches for the
container's real exit code — the lease-renewal loop in ``agent.main``
continues concurrently on the asyncio event loop, independent of how long the
container takes to finish.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker

from agent.runtime.docker_launcher import (
    RendezvousSpec,
    SupportsLogs,
    TrainerLaunchConfig,
    build_run_kwargs,
    ensure_dataset_cache_volume,
    ensure_rendezvous_network,
    launch_trainer_container,
    stop_container,
    stream_container_logs,
    wait_for_exit,
)
from agent.runtime.metrics import parse_final_line, parse_metric_line, parse_resume_line
from agent.runtime.stream_client import LeaseStreamClient, http_to_ws_base, stream_url
from agent.runtime.subprocess_launcher import (
    TrainerSourceNotFoundError,
    launch_trainer_process,
    parse_memory_limit,
)

logger = logging.getLogger("agent.runtime.execution")

#: Sentinel placed on the log queue once the container's real log stream ends.
_LOG_DONE = object()


class DockerLaunchError(Exception):
    """The trainer failed to launch at all (missing image, GPU device request
    rejected, daemon unreachable, bad mount, missing trainer source on the
    unsandboxed path, ...) — no training happened, so the caller must fail the
    lease honestly rather than report an empty result."""


async def _launch_unsandboxed(
    *,
    run_kwargs: dict[str, Any],
    launch_config: TrainerLaunchConfig,
    rendezvous: RendezvousSpec | None,
) -> SupportsLogs:
    """Start the trainer as a child process (ADR-007 addendum).

    Reuses ``run_kwargs["environment"]`` — the very dict the container path
    passes to Docker — so the trainer is configured identically on both
    backends and they cannot drift.
    """
    if rendezvous is not None and rendezvous.world_size > 1:
        # A multi-rank cohort needs the c10d rendezvous network the container
        # path builds. Refused rather than silently degraded to one rank, which
        # would report a world_size=N result that was never distributed.
        raise DockerLaunchError(
            "multi-rank (world_size > 1) jobs require the container path; this "
            "node is running unsandboxed and can only take single-rank work"
        )
    environment: dict[str, str] = dict(run_kwargs["environment"])
    try:
        return await asyncio.to_thread(
            launch_trainer_process,
            environment=environment,
            data_cache_dir=Path(launch_config.unsandboxed_data_cache_dir),
            memory_limit_bytes=parse_memory_limit(launch_config.memory_limit),
        )
    except TrainerSourceNotFoundError as exc:
        raise DockerLaunchError(str(exc)) from exc
    except Exception as exc:
        raise DockerLaunchError(f"could not start the trainer process: {exc}") from exc


@dataclass
class ExecutionResult:
    """The real outcome of running one lease's trainer container."""

    exit_code: int
    final_loss: float | None
    final_test_accuracy: float | None
    epochs_completed: int
    device: str | None

    def as_result_payload(self) -> dict[str, Any]:
        """The ``TrainingResultIn``-shaped dict for the terminal REST call."""
        return {
            "final_loss": self.final_loss,
            "final_test_accuracy": self.final_test_accuracy,
            "epochs_completed": self.epochs_completed,
            "exit_code": self.exit_code,
            "device": self.device,
        }


def _tail_logs_thread(container: SupportsLogs, out_queue: queue.Queue[Any]) -> None:
    """Runs in a worker thread: blocking docker-py log tail -> thread-safe queue."""
    try:
        for stream_name, line in stream_container_logs(container):
            out_queue.put((stream_name, line))
    except Exception as exc:  # noqa: BLE001 - report, never crash the thread silently
        logger.error("log tail thread failed: %s", exc)
    finally:
        out_queue.put(_LOG_DONE)


def _wait_thread(container: SupportsLogs, out_queue: queue.Queue[int]) -> None:
    """Runs in a worker thread: blocking docker-py wait() -> thread-safe queue.

    Started concurrently with the log tail (not after it) so the real exit
    code is captured promptly, before Docker's ``--rm`` auto-removal can make
    the container object stop answering queries.
    """
    try:
        code = wait_for_exit(container)
    except Exception as exc:  # noqa: BLE001
        logger.error("container wait failed: %s", exc)
        code = 1
    out_queue.put(code)


async def run_lease_execution(
    *,
    docker_client: docker.DockerClient | None,
    orchestrator_http_base: str,
    node_id: str,
    access_token: str,
    lease_id: str,
    lease_epoch: int,
    job_id: str,
    job_spec: dict[str, Any],
    has_gpu: bool,
    launch_config: TrainerLaunchConfig,
    rendezvous: RendezvousSpec | None = None,
    unsandboxed: bool = False,
) -> ExecutionResult:
    """Launch this lease's trainer and run it to completion, forwarding its
    real output over the WebSocket stream as it happens.

    ``rendezvous`` (M5): when present with ``world_size > 1`` the container is
    launched under torchrun for real DDP, joining the cohort's shared network
    (created here if absent). ``None``/``world_size == 1`` is the M4
    single-process path. Raises :class:`DockerLaunchError` if the container
    itself never started.

    ``unsandboxed`` (ADR-007 addendum) runs the trainer as a child process
    instead, for a peer with no Docker. Everything below the launch — log
    streaming, metric parsing, cancellation, abandonment — is identical, because
    both backends satisfy the same ``SupportsLogs`` contract.
    """
    run_kwargs = build_run_kwargs(
        config=launch_config,
        job_spec=job_spec,
        job_id=job_id,
        lease_id=lease_id,
        lease_epoch=lease_epoch,
        has_gpu=has_gpu,
        rendezvous=rendezvous,
    )

    container: SupportsLogs
    if unsandboxed:
        container = await _launch_unsandboxed(
            run_kwargs=run_kwargs, launch_config=launch_config, rendezvous=rendezvous
        )
    else:
        assert docker_client is not None, "the container path requires a Docker client"
        ensure_dataset_cache_volume(
            docker_client, name=launch_config.dataset_cache_volume
        )
        if rendezvous is not None and rendezvous.world_size > 1:
            ensure_rendezvous_network(docker_client, name=rendezvous.network)
        try:
            container = await asyncio.to_thread(
                launch_trainer_container, docker_client, run_kwargs=run_kwargs
            )
        except Exception as exc:
            raise DockerLaunchError(str(exc)) from exc

    logger.info(
        "trainer %s launched: id=%s lease=%s job=%s has_gpu=%s rank=%s world_size=%s",
        "subprocess" if unsandboxed else "container",
        getattr(container, "id", "?"),
        lease_id,
        job_id,
        has_gpu,
        rendezvous.rank if rendezvous is not None else 0,
        rendezvous.world_size if rendezvous is not None else 1,
    )

    ws_url = stream_url(
        http_to_ws_base(orchestrator_http_base),
        node_id=node_id,
        lease_id=lease_id,
        epoch=lease_epoch,
    )

    line_queue: queue.Queue[Any] = queue.Queue()
    exit_code_queue: queue.Queue[int] = queue.Queue(maxsize=1)

    tail_thread = threading.Thread(
        target=_tail_logs_thread, args=(container, line_queue), daemon=True
    )
    wait_thread_handle = threading.Thread(
        target=_wait_thread, args=(container, exit_code_queue), daemon=True
    )
    # Start both immediately and concurrently: wait() must be in flight before
    # --rm's auto-removal can race it.
    wait_thread_handle.start()
    tail_thread.start()

    last_metric: dict[str, Any] | None = None
    final: dict[str, Any] | None = None
    loop = asyncio.get_running_loop()

    try:
        return await _stream_to_completion(
            stream_url_=ws_url,
            access_token=access_token,
            line_queue=line_queue,
            exit_code_queue=exit_code_queue,
            tail_thread=tail_thread,
            wait_thread_handle=wait_thread_handle,
            last_metric=last_metric,
            final=final,
            loop=loop,
        )
    except asyncio.CancelledError:
        # We lost the lease this container was running under (cancelled job, or
        # fenced out and reassigned). Stop it so this node stops being occupied
        # by work that now belongs to someone else. Best-effort, then re-raise
        # so the caller still sees the cancellation.
        logger.info(
            "execution cancelled for lease=%s job=%s — stopping abandoned container %s",
            lease_id,
            job_id,
            getattr(container, "id", "?"),
        )
        await asyncio.to_thread(stop_container, container)
        raise


async def _stream_to_completion(
    *,
    stream_url_: str,
    access_token: str,
    line_queue: queue.Queue[Any],
    exit_code_queue: queue.Queue[int],
    tail_thread: threading.Thread,
    wait_thread_handle: threading.Thread,
    last_metric: dict[str, Any] | None,
    final: dict[str, Any] | None,
    loop: asyncio.AbstractEventLoop,
) -> ExecutionResult:
    """Pump the container's real output to the orchestrator until it exits.

    Split out of :func:`run_lease_execution` so the cancellation handler there
    wraps the whole pump — including the blocking exit-code wait — rather than
    only the log loop.
    """
    ws_url = stream_url_
    async with LeaseStreamClient(url=ws_url, access_token=access_token) as stream:
        while True:
            item = await loop.run_in_executor(None, line_queue.get)
            if item is _LOG_DONE:
                break
            stream_name, line = item
            await stream.send_log(stream=stream_name, line=line)

            if stream_name != "stdout":
                continue
            metric = parse_metric_line(line)
            if metric is not None:
                last_metric = metric
                await stream.send_metric(
                    epoch=metric["epoch"],
                    loss=metric["loss"],
                    test_accuracy=metric["test_accuracy"],
                    step=metric.get("step"),
                )
                continue
            resume = parse_resume_line(line)
            if resume is not None:
                # M6: the trainer loaded a checkpoint and continued — forward it
                # so the orchestrator records a "resumed from checkpoint" event.
                await stream.send_resume(
                    from_step=resume["from_step"],
                    from_epoch=resume["from_epoch"],
                    checkpoint_key=resume["checkpoint_key"],
                )
                continue
            parsed_final = parse_final_line(line)
            if parsed_final is not None:
                final = parsed_final

    exit_code = await loop.run_in_executor(None, exit_code_queue.get)
    tail_thread.join(timeout=5.0)
    wait_thread_handle.join(timeout=5.0)

    if final is not None:
        return ExecutionResult(
            exit_code=exit_code,
            final_loss=final["final_loss"],
            final_test_accuracy=final["final_test_accuracy"],
            epochs_completed=final["epochs_completed"],
            device=final["device"],
        )

    # No final line (e.g. a crash before it printed) — fall back to the last
    # real metric observed rather than fabricating a summary from nothing.
    if last_metric is not None:
        return ExecutionResult(
            exit_code=exit_code,
            final_loss=last_metric["loss"],
            final_test_accuracy=last_metric["test_accuracy"],
            epochs_completed=last_metric["epoch"],
            device=None,
        )
    return ExecutionResult(
        exit_code=exit_code,
        final_loss=None,
        final_test_accuracy=None,
        epochs_completed=0,
        device=None,
    )
