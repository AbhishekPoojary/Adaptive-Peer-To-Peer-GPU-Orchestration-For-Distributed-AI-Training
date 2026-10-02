"""Agent entrypoint: enrollment, JWT refresh, telemetry heartbeat, and real
training execution (M1/M2/M4).

Flow:
  1. If ``--state-dir`` already has a persisted node identity, skip
     enrollment and refresh the JWT via challenge-response (ADR-008 stage 3).
  2. Otherwise, generate an Ed25519 keypair locally (the private key never
     leaves this process' disk), register with the orchestrator using the
     supplied one-time enrollment token and a truthful hardware inventory,
     and persist the resulting identity.
  3. Heartbeat forever at a configurable interval, reporting real CPU/RAM
     (psutil) and GPU (NVML, or ``null`` if unavailable) telemetry plus the
     agent-side RTT EWMA measured around each heartbeat round trip
     (CONTRIBUTING.md #2, #3). The JWT is refreshed before it expires.
  4. After each heartbeat, service this node's lease: claim scheduled work,
     launch the real trainer container for it (ADR-007 isolation, M4 —
     ``agent.runtime.execution``), keep the lease alive by timer while the
     container runs however long it takes, and report the real outcome via
     complete/fail once it exits.

Every number reported here is either read from real hardware/OS interfaces or
is ``None`` — nothing is ever invented to fill a gap.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import docker
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent import __version__ as _agent_version
from agent.leases import (
    RELEASE_REASON,
    HeldLease,
    MissingJobSpecError,
    claim_lease,
    complete_lease,
    fail_lease,
    held_from_response,
    job_spec_from_claim,
    renew_lease,
)
from agent.metrics_server import AgentMetricsState, start_metrics_server
from agent.runtime.docker_launcher import (
    RendezvousSpec,
    TrainerLaunchConfig,
    refresh_trainer_image,
    trainer_image_available,
)
from agent.runtime.execution import DockerLaunchError, ExecutionResult, run_lease_execution
from agent.telemetry.latency import RttEwma, Stopwatch
from agent.telemetry.nvml import GpuInventoryEntry, GpuTelemetryEntry, collect_gpu_telemetry
from agent.telemetry.nvml import collect_gpu_inventory as _collect_gpu_inventory
from agent.telemetry.system import collect_host_inventory, collect_system_telemetry
from agent.updates import UPDATE_EXIT_CODE, RestartForUpdate, newer_bundle_version

logger = logging.getLogger("agent")

_STATE_FILENAME = "state.json"
# Refresh the access token once fewer than this many seconds remain on it.
_REFRESH_MARGIN_SECONDS = 60.0


# --- Persisted identity -------------------------------------------------------


@dataclass(frozen=True)
class AgentState:
    """This node's persisted identity: server-assigned id/name plus the
    Ed25519 private key generated on first run. The private key never leaves
    this file."""

    node_id: str
    name: str
    private_key_pem: str


def _state_path(state_dir: Path) -> Path:
    return state_dir / _STATE_FILENAME


def load_state(state_dir: Path) -> AgentState | None:
    """Load a previously persisted identity, or None if this is a first run."""
    path = _state_path(state_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return AgentState(
        node_id=data["node_id"], name=data["name"], private_key_pem=data["private_key_pem"]
    )


def save_state(state_dir: Path, state: AgentState) -> None:
    """Persist the node identity. Restricts file permissions on POSIX.

    On Windows there is no direct equivalent of POSIX file-mode bits; the
    file is protected only by whatever NTFS ACLs the parent directory
    inherits. This is a documented limitation, not a silent gap: it is
    logged every time state is written.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(state_dir)
    path.write_text(
        json.dumps(
            {
                "node_id": state.node_id,
                "name": state.name,
                "private_key_pem": state.private_key_pem,
            }
        ),
        encoding="utf-8",
    )
    if os.name == "posix":
        os.chmod(path, 0o600)
    else:
        logger.warning(
            "state file %s holds the node's private key in cleartext; on Windows "
            "this process cannot restrict file permissions the way POSIX chmod "
            "0600 does — restrict access to this directory manually if this is a "
            "shared machine.",
            path,
        )


def _generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_key, public_pem


def _private_key_to_pem(private_key: Ed25519PrivateKey) -> str:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def _load_private_key(pem: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


# --- Hardware inventory / telemetry payload construction ---------------------


def build_hardware_inventory() -> dict[str, Any]:
    """Truthful hardware inventory for POST /nodes/register.

    GPU inventory is [] when NVML is unavailable or reports no devices —
    never a fabricated entry. NVML absence is logged explicitly, not silently
    swallowed.
    """
    host = collect_host_inventory()
    gpus = _collect_gpu_inventory()
    if gpus is None:
        logger.info("NVML unavailable at enrollment: reporting hardware.gpus = []")
        gpu_list: list[GpuInventoryEntry] = []
    else:
        gpu_list = gpus
    return {
        "hostname": host.hostname,
        "os": host.os,
        "cpu_model": host.cpu_model,
        "cores": host.cores,
        "ram_bytes": host.ram_bytes,
        "gpus": [
            {"name": g.name, "vram_bytes": g.vram_bytes, "driver_version": g.driver_version}
            for g in gpu_list
        ],
    }


def build_heartbeat_payload(*, rtt_ms: float | None) -> dict[str, Any]:
    """Real CPU/RAM (+ GPU-or-null) telemetry for POST /nodes/{id}/heartbeat."""
    system = collect_system_telemetry()
    gpu_tel = collect_gpu_telemetry()
    if gpu_tel is None:
        logger.info("NVML unavailable this cycle: reporting gpu = null")
        gpu_payload: list[dict[str, Any]] | None = None
    else:
        gpu_payload = [_gpu_telemetry_dict(g) for g in gpu_tel]
    return {
        "cpu_percent": system.cpu_percent,
        "ram_used_bytes": system.ram_used_bytes,
        "ram_total_bytes": system.ram_total_bytes,
        "gpu": gpu_payload,
        "rtt_ms": rtt_ms,
    }


def _gpu_telemetry_dict(entry: GpuTelemetryEntry) -> dict[str, Any]:
    return {
        "util_percent": entry.util_percent,
        "mem_used_bytes": entry.mem_used_bytes,
        "mem_total_bytes": entry.mem_total_bytes,
        "temperature_c": entry.temperature_c,
        "power_w": entry.power_w,
    }


# --- Orchestrator calls -------------------------------------------------------


class EnrollmentError(Exception):
    """Registration with the orchestrator failed."""


async def register(
    client: httpx.AsyncClient,
    *,
    orchestrator: str,
    enrollment_token: str,
    public_key_pem: str,
    hardware: dict[str, Any],
) -> tuple[AgentState, str, int]:
    """Enroll this node. Returns (state, access_token, expires_in_seconds)."""
    resp = await client.post(
        f"{orchestrator}/nodes/register",
        json={
            "enrollment_token": enrollment_token,
            "public_key": public_key_pem,
            "hardware": hardware,
            "agent_version": _agent_version,
        },
    )
    if resp.status_code != 201:
        raise EnrollmentError(f"registration failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    return (
        AgentState(node_id=data["node_id"], name=data["name"], private_key_pem=""),
        data["access_token"],
        int(data["expires_in"]),
    )


def _describe(exc: BaseException) -> str:
    """Render an exception for a log line, never as an empty string.

    Several httpx errors stringify to nothing -- a bare ``ConnectTimeout`` is
    the common one -- so a plain ``%s`` printed "enrollment failed:" with the
    reason missing, which is exactly the moment a peer most needs one. Name the
    type always; append the message only when there is one.
    """
    message = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


class TokenRefreshError(Exception):
    """Challenge-response JWT refresh failed."""


async def refresh_token(
    client: httpx.AsyncClient,
    *,
    orchestrator: str,
    node_id: str,
    private_key: Ed25519PrivateKey,
) -> tuple[str, int]:
    """Prove key possession via challenge-response. Returns (access_token, expires_in)."""
    chal_resp = await client.post(f"{orchestrator}/auth/challenge", json={"node_id": node_id})
    if chal_resp.status_code != 200:
        raise TokenRefreshError(f"challenge request failed ({chal_resp.status_code})")
    nonce = chal_resp.json()["nonce"]
    signature = base64.b64encode(private_key.sign(nonce.encode("utf-8"))).decode("ascii")

    refresh_resp = await client.post(
        f"{orchestrator}/auth/token/refresh",
        json={"node_id": node_id, "nonce": nonce, "signature": signature},
    )
    if refresh_resp.status_code != 200:
        raise TokenRefreshError(f"token refresh failed ({refresh_resp.status_code})")
    data = refresh_resp.json()
    return data["access_token"], int(data["expires_in"])


async def send_heartbeat(
    client: httpx.AsyncClient,
    *,
    orchestrator: str,
    node_id: str,
    access_token: str,
    rtt_ms: float | None,
) -> tuple[dict[str, Any], float]:
    """Send one heartbeat. Returns (response body, measured RTT in ms)."""
    payload = build_heartbeat_payload(rtt_ms=rtt_ms)
    with Stopwatch() as sw:
        resp = await client.post(
            f"{orchestrator}/nodes/{node_id}/heartbeat",
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    resp.raise_for_status()
    assert sw.elapsed_ms is not None
    return resp.json(), sw.elapsed_ms


# --- CLI / main loop -----------------------------------------------------------


@dataclass
class ExecutingLease:
    """A lease this agent claimed and is (or was) running a real trainer
    container for (M4). Wraps the held-lease bookkeeping (renewal timing,
    which runs on a timer independent of training progress) with the
    background asyncio task actually executing it
    (``agent.runtime.execution.run_lease_execution``).

    ``task`` is ``None`` only on the ``--release-after`` test-only path, which
    holds and releases a lease without ever launching a container (used by
    tests that exercise the renew/release mechanics without needing Docker).
    """

    held: HeldLease
    task: asyncio.Task[ExecutionResult] | None
    #: Set by the execution task the moment the trainer is really running.
    #: The orchestrator records LEASED -> RUNNING on a lease's first renewal,
    #: and renewal is otherwise timer-driven (``lease_ttl - margin`` after the
    #: claim, ~10 s with a 15 s TTL) -- so without this, "training started" was
    #: stamped seconds after training had in fact started, and every recovery
    #: measurement silently included that bookkeeping lag.
    launched: asyncio.Event = field(default_factory=asyncio.Event)
    #: Whether the launch-time renewal has been sent. One-shot: later renewals
    #: go back to the timer.
    start_reported: bool = False

    def start_report_due(self) -> bool:
        return self.task is not None and self.launched.is_set() and not self.start_reported


async def _keep_trainer_image_fresh(
    docker_client: docker.DockerClient, image: str, *, interval_seconds: float
) -> None:
    """Re-pull the trainer image periodically for as long as the agent runs.

    Peers leave the agent running for days, so a check only at startup would
    let a fixed trainer wait for a reboot. The pull runs in a worker thread so
    heartbeats carry on during a large download, and it is safe while a job
    is training: the running container holds the image it started with, and
    the next job starts on the new one.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        outcome = await asyncio.to_thread(refresh_trainer_image, docker_client, image)
        if outcome == "updated":
            logger.info("trainer image %s updated; the next job uses it", image)
        elif outcome == "failed":
            logger.info("trainer image refresh failed; will retry in %.0fh",
                        interval_seconds / 3600)


def _node_has_gpu() -> bool:
    """Fresh, live NVML check at launch time (never a cached, possibly-stale
    enrollment-time value) — the only thing that decides whether the trainer
    container is launched with a GPU device request."""
    gpus = _collect_gpu_inventory()
    return bool(gpus)


def _rendezvous_spec(rendezvous: dict[str, Any] | None) -> RendezvousSpec | None:
    """Build the launch-side ``RendezvousSpec`` from the claim response's
    ``rendezvous`` block (M5). ``None`` (or a missing block) means the M4
    single-process path — the container is launched without torchrun."""
    if rendezvous is None:
        return None
    return RendezvousSpec(
        world_size=int(rendezvous["world_size"]),
        rank=int(rendezvous["rank"]),
        is_rendezvous_host=bool(rendezvous["is_rendezvous_host"]),
        backend=str(rendezvous["backend"]),
        endpoint=str(rendezvous["endpoint"]),
        rdzv_id=str(rendezvous["rdzv_id"]),
        network=str(rendezvous["network"]),
        host_alias=str(rendezvous["host_alias"]),
        max_restarts=int(rendezvous["max_restarts"]),
    )


async def _report_result(
    client: httpx.AsyncClient,
    *,
    orchestrator: str,
    held: HeldLease,
    access_token: str,
    result: ExecutionResult | None,
    failure_reason: str | None,
) -> None:
    """Report a lease's real terminal outcome: complete on a real exit 0,
    fail otherwise (including when there is no container result at all, e.g.
    Docker itself never launched). Never pretends success."""
    payload = result.as_result_payload() if result is not None else None
    try:
        if result is not None and result.exit_code == 0:
            await complete_lease(
                client,
                orchestrator=orchestrator,
                lease_id=held.lease_id,
                lease_epoch=held.lease_epoch,
                access_token=access_token,
                result=payload,
            )
            logger.info(
                "lease id=%s completed: final_test_accuracy=%s epochs_completed=%s",
                held.lease_id,
                result.final_test_accuracy,
                result.epochs_completed,
            )
        else:
            reason = failure_reason or (
                f"trainer exited with code {result.exit_code}"
                if result is not None
                else "unknown execution failure"
            )
            await fail_lease(
                client,
                orchestrator=orchestrator,
                lease_id=held.lease_id,
                lease_epoch=held.lease_epoch,
                reason=reason,
                access_token=access_token,
                result=payload,
            )
            logger.info("lease id=%s failed: %s", held.lease_id, reason)
    except httpx.HTTPStatusError as exc:
        # A stale/fenced epoch (409) here means the job was reassigned while
        # this container ran — the correct outcome is exactly to let this
        # report be rejected, not to retry as if we still owned the lease.
        logger.warning(
            "reporting outcome for lease id=%s was rejected (%s) — likely fenced out",
            held.lease_id,
            exc.response.status_code,
        )
    except httpx.HTTPError as exc:
        logger.error(
            "reporting outcome for lease id=%s failed (network): %s", held.lease_id, exc
        )


#: Execution tasks cancelled after their lease was lost, still stopping their
#: container. Module-level because they outlive the call that cancelled them,
#: and referenced so the event loop cannot collect them mid-stop.
_draining: set[asyncio.Task[ExecutionResult]] = set()


async def _service_lease(
    client: httpx.AsyncClient,
    *,
    orchestrator: str,
    node_id: str,
    access_token: str,
    executing: ExecutingLease | None,
    renew_margin_seconds: float,
    release_after: float | None,
    docker_client: docker.DockerClient | None,
    launch_config: TrainerLaunchConfig,
    allow_unsandboxed: bool = False,
) -> ExecutingLease | None:
    """Advance the lease state machine for one cycle.

    With no lease held: poll claim, then (Docker permitting) launch the real
    trainer container as a background task and start tracking it. With a
    lease held: if its execution task has finished, report the real outcome
    (complete/fail) and free the slot; otherwise keep the lease alive by
    renewing on schedule while the container keeps running, however long that
    takes — renewal is timer-driven, independent of training progress.
    """
    if executing is None:
        if _draining:
            # An abandoned trainer is still being stopped. Taking new work now
            # would start a second trainer on a GPU the first still holds.
            return None
        try:
            claimed = await claim_lease(
                client, orchestrator=orchestrator, node_id=node_id, access_token=access_token
            )
        except httpx.HTTPError as exc:
            logger.error("lease claim failed: %s", exc)
            return None
        if claimed is None:
            return None
        lease = claimed["lease"]
        rendezvous = _rendezvous_spec(claimed.get("rendezvous"))
        held = held_from_response(lease)
        logger.info(
            "lease claimed: id=%s epoch=%d job=%s rank=%s world_size=%s",
            held.lease_id,
            held.lease_epoch,
            held.job_id,
            rendezvous.rank if rendezvous is not None else 0,
            rendezvous.world_size if rendezvous is not None else 1,
        )

        if release_after is not None:
            # Test-only path: hold-and-release, never executes (no Docker
            # needed) — see ExecutingLease.task's docstring.
            return ExecutingLease(held=held, task=None)

        if docker_client is None and not allow_unsandboxed:
            logger.error(
                "cannot execute lease id=%s: Docker is unavailable on this node "
                "(pass --allow-unsandboxed to run the trainer as a child process "
                "instead; see docs/adr/ADR-007-addendum.md for what that gives up)",
                held.lease_id,
            )
            await _report_result(
                client,
                orchestrator=orchestrator,
                held=held,
                access_token=access_token,
                result=None,
                failure_reason="docker unavailable on this node",
            )
            return None

        try:
            job_spec = job_spec_from_claim(claimed)
        except MissingJobSpecError as exc:
            logger.error("no job spec in the grant for job=%s: %s", held.job_id, exc)
            await _report_result(
                client,
                orchestrator=orchestrator,
                held=held,
                access_token=access_token,
                result=None,
                failure_reason=str(exc),
            )
            return None

        has_gpu = _node_has_gpu()
        logger.info(
            "launching trainer container for lease id=%s job=%s has_gpu=%s spec=%s",
            held.lease_id,
            held.job_id,
            has_gpu,
            job_spec,
        )
        launched = asyncio.Event()
        task: asyncio.Task[ExecutionResult] | None = asyncio.create_task(
            run_lease_execution(
                docker_client=docker_client,
                orchestrator_http_base=orchestrator,
                node_id=node_id,
                access_token=access_token,
                lease_id=held.lease_id,
                lease_epoch=held.lease_epoch,
                job_id=held.job_id,
                job_spec=job_spec,
                has_gpu=has_gpu,
                launch_config=launch_config,
                rendezvous=rendezvous,
                unsandboxed=docker_client is None,
                launched=launched,
                checkpoint_token=claimed.get("checkpoint_token"),
            )
        )
        return ExecutingLease(held=held, task=task, launched=launched)

    held = executing.held
    task = executing.task

    # Voluntary release for automated testing only (task is None here).
    if release_after is not None and held.seconds_held() >= release_after:
        try:
            await fail_lease(
                client,
                orchestrator=orchestrator,
                lease_id=held.lease_id,
                lease_epoch=held.lease_epoch,
                reason=RELEASE_REASON,
                access_token=access_token,
            )
            logger.info(
                "released lease id=%s after %.1fs via /fail (reason=%s)",
                held.lease_id,
                held.seconds_held(),
                RELEASE_REASON,
            )
        except httpx.HTTPError as exc:
            logger.error("lease release failed: %s", exc)
        return None

    if task is not None and task.done():
        try:
            result = task.result()
        except DockerLaunchError as exc:
            logger.error("docker launch failed for lease id=%s: %s", held.lease_id, exc)
            await _report_result(
                client,
                orchestrator=orchestrator,
                held=held,
                access_token=access_token,
                result=None,
                failure_reason=f"docker launch failed: {exc}",
            )
            return None
        except Exception as exc:  # noqa: BLE001 - bug in the orchestration itself
            logger.error("lease execution crashed for lease id=%s: %s", held.lease_id, exc)
            await _report_result(
                client,
                orchestrator=orchestrator,
                held=held,
                access_token=access_token,
                result=None,
                failure_reason=f"agent execution error: {exc}",
            )
            return None

        await _report_result(
            client,
            orchestrator=orchestrator,
            held=held,
            access_token=access_token,
            result=result,
            failure_reason=None,
        )
        return None

    start_report = executing.start_report_due()
    if start_report or held.renewal_due(margin_seconds=renew_margin_seconds):
        try:
            renewed = await renew_lease(
                client,
                orchestrator=orchestrator,
                lease_id=held.lease_id,
                lease_epoch=held.lease_epoch,
                access_token=access_token,
            )
            held.expires_at_ts = datetime.fromisoformat(renewed["expires_at"]).timestamp()
            if start_report:
                executing.start_reported = True
            logger.info(
                "renewed lease id=%s epoch=%d (new expiry=%s)%s",
                held.lease_id,
                held.lease_epoch,
                renewed["expires_at"],
                " — trainer still running" if task is not None else " — still just holding",
            )
        except httpx.HTTPStatusError as exc:
            # 409 = fenced/expired: we lost this lease. Epoch fencing already
            # guarantees a late report from this run would be rejected, so the
            # orchestrator's data is safe either way — but leaving the container
            # running would keep this node busy on work that now belongs to
            # someone else, so it could not claim anything new while the
            # scheduler kept offering it slots that expire unclaimed. Abandon it.
            logger.warning(
                "lease id=%s renew rejected (%s): job was reassigned — abandoning this attempt",
                held.lease_id,
                exc.response.status_code,
            )
            if task is not None and not task.done():
                # Cancelling stops the container, which can take seconds. It
                # is NOT awaited here: this function runs inside the heartbeat
                # loop, and awaiting it once silenced a node for 11 s -- long
                # enough for the failure detector to declare a healthy node
                # dead (48 times in one scalability run). The task finishes
                # in the background; claims wait for it (see below).
                task.cancel()
                _draining.add(task)
                task.add_done_callback(_draining.discard)
            return None
        except httpx.HTTPError as exc:
            logger.error("lease renew failed (network): %s", exc)
    return executing


def _handle_missing_trainer_image(
    *, image: str, allow_unsandboxed: bool, memory_limit: str
) -> docker.DockerClient | None:
    """Decide what a Docker-capable peer does when it has no trainer image.

    The image is built locally and pushed to no registry, so a peer that has
    never built it cannot obtain it. Returning ``None`` routes execution down
    the unsandboxed path (the caller treats a null client that way), which is
    both available and lighter here — building the image pulls a ~7 GB CUDA
    base, while the subprocess path needs only PyTorch.

    Without consent there is nothing safe to fall back to, so the node stays
    enrolled and honest: it will fail claimed leases rather than pretend.
    """
    if allow_unsandboxed:
        logger.warning(
            "Docker is available but the trainer image %s is not on this machine, "
            "and it is published to no registry — so Docker cannot run it. "
            "Falling back to the unsandboxed path, which was permitted.",
            image,
        )
        _log_unsandboxed_consent(
            RuntimeError(f"trainer image {image} is not present"), memory_limit
        )
        return None

    logger.error(
        "\n"
        "  ────────────────────────────────────────────────────────────────\n"
        "  CANNOT RUN JOBS: the trainer image is missing\n"
        "  ────────────────────────────────────────────────────────────────\n"
        "  Docker works here, but the image %s is not on this machine and is\n"
        "  published to no registry, so Docker cannot pull it. Every claimed\n"
        "  lease will fail until this is fixed.\n"
        "\n"
        "  Pick one:\n"
        "\n"
        "  1. Run without a container (smaller download, no image needed):\n"
        "       re-run the installer and accept the unsandboxed option, or add\n"
        "       --allow-unsandboxed to the agent command.\n"
        "\n"
        "  2. Build the image yourself from the files already downloaded:\n"
        "       docker build -t %s \\\n"
        "         -f ~/.gpu-orchestrator-agent-src/trainer/Dockerfile \\\n"
        "         ~/.gpu-orchestrator-agent-src\n"
        "     (pulls a ~7 GB CUDA base image the first time)\n"
        "  ────────────────────────────────────────────────────────────────",
        image,
        image,
    )
    return None


def _log_unsandboxed_consent(docker_error: Exception, memory_limit: str) -> None:
    """State plainly what running without a container gives up (ADR-007 addendum).

    Printed every start, not once, and itemised rather than summarised as
    "reduced isolation". The machine at risk usually belongs to someone doing
    the operator a favour, and a warning vague enough to skim is not consent.
    """
    logger.warning(
        "\n"
        "  ────────────────────────────────────────────────────────────────\n"
        "  RUNNING WITHOUT CONTAINER ISOLATION (--allow-unsandboxed)\n"
        "  ────────────────────────────────────────────────────────────────\n"
        "  Docker is not available here (%s), so training jobs will run as\n"
        "  ordinary child processes of this agent, under your user account.\n"
        "\n"
        "  That means a training job on this machine:\n"
        "    - runs with your permissions, not a container's\n"
        "    - can read and write any file you can\n"
        "    - has your network access\n"
        "    - is NOT limited by cgroups\n"
        "\n"
        "  Partial mitigation: memory use is polled and the job is killed if it\n"
        "  exceeds %s. That is sampled, not kernel-enforced, so a sudden spike\n"
        "  can overshoot before it is caught.\n"
        "\n"
        "  The only program run this way is this project's own trainer\n"
        "  (trainer/train.py) — you can read it before agreeing. Job submitters\n"
        "  cannot supply code; only a dataset name from a fixed list and\n"
        "  range-checked numbers.\n"
        "\n"
        "  Install Docker to get full isolation back. Ctrl+C now to stop.\n"
        "  ────────────────────────────────────────────────────────────────",
        docker_error,
        memory_limit,
    )


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m agent", description="GPU orchestrator agent")
    parser.add_argument(
        "--orchestrator", required=True, help="Orchestrator base URL, e.g. http://localhost:8090"
    )
    parser.add_argument(
        "--enrollment-token",
        default=None,
        help="One-time enrollment token (required only for a node's first run)",
    )
    parser.add_argument(
        "--state-dir",
        default=str(Path.home() / ".gpu-orchestrator-agent"),
        help="Directory holding this node's persisted identity",
    )
    parser.add_argument(
        "--heartbeat-interval-seconds",
        type=float,
        default=1.0,
        # 1 s rather than 2 s: the orchestrator's detector needs a few missed
        # beats before it may act, so the cadence sets the floor on detection
        # time (ADR-004 addendum 2). It also sets how quickly an idle node
        # notices an offered lease, since claims ride the heartbeat loop.
        help="Heartbeat cadence",
    )
    parser.add_argument(
        "--bundle-version",
        default=os.environ.get("AGENT_BUNDLE_VERSION"),
        help=(
            "Version of the bundle this agent was installed from (the installer "
            "passes it). Unset disables self-update, as for a developer checkout."
        ),
    )
    parser.add_argument(
        "--update-check-minutes",
        type=float,
        default=float(os.environ.get("AGENT_UPDATE_CHECK_MINUTES", "30")),
        help="How often, while idle, to check for a newer agent (0 disables)",
    )
    parser.add_argument(
        "--trainer-image-refresh-hours",
        type=float,
        default=float(os.environ.get("TRAINER_IMAGE_REFRESH_HOURS", "6")),
        help="How often to pull a newer published trainer image (0 disables)",
    )
    parser.add_argument(
        "--rtt-ewma-alpha",
        type=float,
        default=0.3,
        help="Smoothing factor for the agent-side RTT EWMA (0 < alpha <= 1)",
    )
    parser.add_argument(
        "--lease-renew-margin-seconds",
        type=float,
        default=5.0,
        help="Renew a held lease once fewer than this many seconds remain on it",
    )
    parser.add_argument(
        "--release-after",
        type=float,
        default=None,
        help=(
            "TEST ONLY: after holding a lease this many seconds, release it via "
            f"/fail with reason '{RELEASE_REASON}' without ever launching a "
            "trainer container. Default: claim, execute, and report a real outcome."
        ),
    )
    parser.add_argument(
        "--trainer-image",
        default=os.environ.get("TRAINER_IMAGE", "gpu-orchestrator-trainer:latest"),
        help="Docker image the agent launches for training jobs (ADR-007).",
    )
    parser.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help=(
            "If Docker is unavailable, run the trainer as a child process instead "
            "of a container. This gives up the ADR-007 isolation guarantees — see "
            "docs/adr/ADR-007-addendum.md. Off by default."
        ),
    )
    parser.add_argument(
        "--trainer-memory-limit",
        default=os.environ.get("TRAINER_MEMORY_LIMIT", "6g"),
        help="--memory limit applied to the trainer container.",
    )
    parser.add_argument(
        "--trainer-pids-limit",
        type=int,
        default=int(os.environ.get("TRAINER_PIDS_LIMIT", "256")),
        help="--pids-limit applied to the trainer container.",
    )
    parser.add_argument(
        "--dataset-cache-volume",
        default=os.environ.get("DATASET_CACHE_VOLUME", "gpu-orchestrator-dataset-cache"),
        help="Named Docker volume mounted at /data-cache, created once per node if absent.",
    )
    # M6 (ADR-006): checkpoint-to-MinIO config passed through to trainer
    # containers. Defaults come from env so a co-located dev node inherits the
    # same S3_* the compose stack sets. Absent endpoint/creds → no checkpointing.
    parser.add_argument(
        "--s3-endpoint-url",
        default=os.environ.get("S3_ENDPOINT_URL"),
        help="S3/MinIO endpoint the trainer writes checkpoints to (M6, ADR-006).",
    )
    parser.add_argument(
        "--s3-access-key", default=os.environ.get("S3_ACCESS_KEY"), help="S3 access key."
    )
    parser.add_argument(
        "--s3-secret-key", default=os.environ.get("S3_SECRET_KEY"), help="S3 secret key."
    )
    parser.add_argument(
        "--s3-bucket-checkpoints",
        default=os.environ.get("S3_BUCKET_CHECKPOINTS", "checkpoints"),
        help="S3 bucket for checkpoints.",
    )
    parser.add_argument(
        "--s3-region", default=os.environ.get("S3_REGION", "us-east-1"), help="S3 region."
    )
    parser.add_argument(
        "--checkpoint-every-n-steps",
        type=int,
        default=int(os.environ.get("CHECKPOINT_EVERY_N_STEPS", "100")),
        help="Rank-0 checkpoint cadence in optimizer steps (M6).",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=None,
        help=(
            "Expose a Prometheus /metrics HTTP server on this port (real "
            "heartbeats/RTT/lease-state/GPU gauges only). Off by default — "
            "no port is opened unless this is set."
        ),
    )
    return parser.parse_args(argv)


def plaintext_warning(orchestrator: str) -> str | None:
    """Why this connection is unencrypted, or None when it is fine.

    Plain HTTP is fine to this machine itself and over Tailscale, which
    encrypts every packet on its own (100.64.0.0/10 addresses, *.ts.net names).
    Anywhere else -- a LAN address, a public IP -- this node's token, its
    heartbeats, the job's logs and its checkpoints cross the network readable
    by anyone on the path.
    """
    import ipaddress
    from urllib.parse import urlsplit

    parts = urlsplit(orchestrator)
    host = parts.hostname or ""
    if parts.scheme == "https" or host in ("localhost", "host.docker.internal"):
        return None
    if host.endswith(".ts.net"):
        return None
    try:
        address = ipaddress.ip_address(host)
        if address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10"):
            return None
    except ValueError:
        pass
    return (
        f"{orchestrator} is plain HTTP to another machine: this node's access token, "
        "telemetry, training logs and checkpoints are sent unencrypted. Ask the "
        "operator for the https:// address (demo.ps1 -Public prints one) or join "
        "over Tailscale."
    )


async def run(args: argparse.Namespace) -> None:
    orchestrator = args.orchestrator.rstrip("/")
    warning = plaintext_warning(orchestrator)
    if warning:
        logger.warning("UNENCRYPTED CONNECTION: %s", warning)
    state_dir = Path(args.state_dir)
    state = load_state(state_dir)

    async with httpx.AsyncClient(timeout=10.0) as client:
        if state is None:
            if not args.enrollment_token:
                logger.error(
                    "no persisted identity in %s and no --enrollment-token given; "
                    "cannot enroll",
                    state_dir,
                )
                raise SystemExit(1)
            private_key, public_pem = _generate_keypair()
            hardware = build_hardware_inventory()
            logger.info(
                "enrolling: hostname=%s cpu=%s gpus=%d",
                hardware["hostname"],
                hardware["cpu_model"],
                len(hardware["gpus"]),
            )
            try:
                new_state, access_token, expires_in = await register(
                    client,
                    orchestrator=orchestrator,
                    enrollment_token=args.enrollment_token,
                    public_key_pem=public_pem,
                    hardware=hardware,
                )
            except (EnrollmentError, httpx.HTTPError) as exc:
                logger.error(
                    "enrollment failed (orchestrator=%s): %s",
                    orchestrator,
                    _describe(exc),
                )
                raise SystemExit(1) from exc
            state = AgentState(
                node_id=new_state.node_id,
                name=new_state.name,
                private_key_pem=_private_key_to_pem(private_key),
            )
            save_state(state_dir, state)
            expires_at = time.time() + expires_in
            logger.info("enrolled as %s (node_id=%s)", state.name, state.node_id)
        else:
            private_key = _load_private_key(state.private_key_pem)
            try:
                access_token, expires_in = await refresh_token(
                    client,
                    orchestrator=orchestrator,
                    node_id=state.node_id,
                    private_key=private_key,
                )
            except (TokenRefreshError, httpx.HTTPError) as exc:
                logger.error(
                    "initial token refresh failed (orchestrator=%s): %s",
                    orchestrator,
                    _describe(exc),
                )
                raise SystemExit(1) from exc
            expires_at = time.time() + expires_in
            logger.info(
                "resuming as %s (node_id=%s) from %s", state.name, state.node_id, state_dir
            )

        rtt_tracker = RttEwma(alpha=args.rtt_ewma_alpha)
        executing_lease: ExecutingLease | None = None
        launch_config = TrainerLaunchConfig(
            image=args.trainer_image,
            memory_limit=args.trainer_memory_limit,
            pids_limit=args.trainer_pids_limit,
            dataset_cache_volume=args.dataset_cache_volume,
            s3_endpoint_url=args.s3_endpoint_url,
            s3_access_key=args.s3_access_key,
            s3_secret_key=args.s3_secret_key,
            s3_bucket_checkpoints=args.s3_bucket_checkpoints,
            s3_region=args.s3_region,
            checkpoint_every_n_steps=args.checkpoint_every_n_steps,
        )
        if launch_config.checkpointing_enabled():
            logger.info(
                "checkpoint-to-MinIO enabled: endpoint=%s bucket=%s every=%d steps",
                launch_config.s3_endpoint_url,
                launch_config.s3_bucket_checkpoints,
                launch_config.checkpoint_every_n_steps,
            )
        else:
            logger.info("checkpoint-to-MinIO not configured; trainer will run without it")

        metrics_state = AgentMetricsState()
        if args.metrics_port is not None:
            start_metrics_server(args.metrics_port, metrics_state)
            logger.info("metrics server listening on :%d/metrics", args.metrics_port)

        docker_client: docker.DockerClient | None = None
        if args.release_after is None:
            try:
                docker_client = docker.from_env()
                docker_client.ping()
                # Docker being installed is not the same fact as Docker being
                # able to run our trainer. The image is built locally and
                # published to no registry, so on a peer that has never built
                # it containers.run() tries to PULL and dies with a registry
                # 404 that reads like an auth failure. Check now and say what
                # is really wrong, rather than failing every claimed lease with
                # "pull access denied".
                # Fetch the newest published trainer before deciding whether
                # this node can run jobs: on a fresh install this is what gets
                # the image at all, and afterwards it is what delivers fixes.
                outcome = refresh_trainer_image(docker_client, launch_config.image)
                if outcome != "skipped":
                    logger.info("trainer image %s: %s", launch_config.image, outcome)
                if trainer_image_available(docker_client, launch_config.image):
                    logger.info(
                        "Docker reachable and trainer image %s present: this node "
                        "can execute training jobs in containers",
                        launch_config.image,
                    )
                else:
                    docker_client = _handle_missing_trainer_image(
                        image=launch_config.image,
                        allow_unsandboxed=args.allow_unsandboxed,
                        memory_limit=launch_config.memory_limit,
                    )
            except Exception as exc:  # noqa: BLE001 - any Docker SDK/daemon failure
                if args.allow_unsandboxed:
                    _log_unsandboxed_consent(exc, launch_config.memory_limit)
                else:
                    logger.error(
                        "Docker is unavailable on this node (%s); any claimed lease will "
                        "be failed honestly rather than executed. To contribute this "
                        "machine without Docker, re-run with --allow-unsandboxed (see "
                        "docs/adr/ADR-007-addendum.md for what that gives up).",
                        exc,
                    )
                docker_client = None

        # Held for the life of the process: the event loop keeps only a weak
        # reference to a task, so an unreferenced one can be collected mid-run.
        background_tasks: set[asyncio.Task[None]] = set()
        if docker_client is not None and args.trainer_image_refresh_hours > 0:
            background_tasks.add(
                asyncio.create_task(
                    _keep_trainer_image_fresh(
                        docker_client,
                        launch_config.image,
                        interval_seconds=args.trainer_image_refresh_hours * 3600,
                    )
                )
            )

        logger.info(
            "starting heartbeat loop: interval=%.1fs orchestrator=%s",
            args.heartbeat_interval_seconds,
            orchestrator,
        )

        # Heartbeats run on their own task. They used to share one loop with
        # lease servicing, so every slow claim or renewal delayed the next
        # heartbeat; over an internet tunnel, where each request costs about a
        # second, that stretched the interval to 2.5 s and made one stall look
        # like a dead node to the failure detector. Token refresh lives here
        # too, and the lease loop below reads whatever token is current.
        async def heartbeat_forever() -> None:
            nonlocal access_token, expires_at
            while True:
                if time.time() > expires_at - _REFRESH_MARGIN_SECONDS:
                    try:
                        access_token, expires_in = await refresh_token(
                            client,
                            orchestrator=orchestrator,
                            node_id=state.node_id,
                            private_key=private_key,
                        )
                        expires_at = time.time() + expires_in
                        logger.info("refreshed access token (expires_in=%ss)", expires_in)
                    except (TokenRefreshError, httpx.HTTPError) as exc:
                        logger.error("token refresh failed, will retry next cycle: %s", exc)

                try:
                    body, rtt_ms = await send_heartbeat(
                        client,
                        orchestrator=orchestrator,
                        node_id=state.node_id,
                        access_token=access_token,
                        rtt_ms=rtt_tracker.value,
                    )
                    agent_ewma = rtt_tracker.observe(rtt_ms)
                    logger.info(
                        "heartbeat ok: status=%s measured_rtt_ms=%.1f agent_rtt_ewma_ms=%.1f "
                        "server_rtt_ewma_ms=%s",
                        body.get("status"),
                        rtt_ms,
                        agent_ewma,
                        body.get("rtt_ewma_ms"),
                    )
                    metrics_state.record_heartbeat_sent(rtt_ewma_ms=agent_ewma)
                    # Same NVML source the heartbeat payload just read from, polled
                    # again here rather than threaded through the payload builder's
                    # return value — a second cheap NVML read per cycle, not a
                    # second data path (CONTRIBUTING.md #2: still real or absent,
                    # never invented).
                    metrics_state.record_gpu_telemetry(collect_gpu_telemetry())
                except httpx.HTTPStatusError as exc:
                    logger.error(
                        "heartbeat rejected (%s): %s", exc.response.status_code, exc.response.text
                    )
                except httpx.HTTPError as exc:
                    logger.error("heartbeat failed (network): %s", exc)
                await asyncio.sleep(args.heartbeat_interval_seconds)

        async def heartbeat_guarded() -> None:
            # A task that dies takes the heartbeat with it, and the node then
            # looks dead while it is still training. Anything unexpected is
            # logged and the heartbeat restarts after one interval.
            while True:
                try:
                    await heartbeat_forever()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("heartbeat loop failed; restarting it")
                    await asyncio.sleep(args.heartbeat_interval_seconds)

        background_tasks.add(asyncio.create_task(heartbeat_guarded()))

        update_every = args.update_check_minutes * 60
        next_update_check = time.monotonic() + update_every
        while True:
            # On the same cadence: poll for / renew / execute a lease. A lease
            # with a running trainer container keeps getting renewed here on
            # schedule regardless of how long training takes; execution
            # itself runs as a background task (see _service_lease).
            executing_lease = await _service_lease(
                client,
                orchestrator=orchestrator,
                node_id=state.node_id,
                access_token=access_token,
                executing=executing_lease,
                renew_margin_seconds=args.lease_renew_margin_seconds,
                release_after=args.release_after,
                docker_client=docker_client,
                launch_config=launch_config,
                allow_unsandboxed=args.allow_unsandboxed,
            )
            metrics_state.record_lease_state(
                lease_active=executing_lease is not None,
                container_running=(
                    executing_lease is not None
                    and executing_lease.task is not None
                    and not executing_lease.task.done()
                ),
            )

            # Only while idle: a job in progress is never interrupted for an
            # update. The check is cheap (one small GET) but there is no reason
            # to make it every second.
            if (
                args.bundle_version
                and update_every > 0
                and executing_lease is None
                and time.monotonic() >= next_update_check
            ):
                next_update_check = time.monotonic() + update_every
                available = await newer_bundle_version(
                    client, orchestrator=orchestrator, installed=args.bundle_version
                )
                if available is not None:
                    raise RestartForUpdate(args.bundle_version, available)

            await asyncio.sleep(args.heartbeat_interval_seconds)


def main() -> None:
    args = parse_args()
    _configure_logging(args.log_level)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("interrupted (Ctrl+C); shutting down cleanly")
    except RestartForUpdate as update:
        logger.info(
            "a newer agent is available (%s -> %s); exiting for the installer "
            "to fetch it and restart this node",
            update.current,
            update.available,
        )
        raise SystemExit(UPDATE_EXIT_CODE) from None


if __name__ == "__main__":
    main()
