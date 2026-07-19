"""Agent entrypoint: enrollment, JWT refresh, and the real telemetry heartbeat
loop (M1).

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent import __version__ as _agent_version
from agent.telemetry.latency import RttEwma, Stopwatch
from agent.telemetry.nvml import GpuInventoryEntry, GpuTelemetryEntry, collect_gpu_telemetry
from agent.telemetry.nvml import collect_gpu_inventory as _collect_gpu_inventory
from agent.telemetry.system import collect_host_inventory, collect_system_telemetry

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
        "--heartbeat-interval-seconds", type=float, default=2.0, help="Heartbeat cadence"
    )
    parser.add_argument(
        "--rtt-ewma-alpha",
        type=float,
        default=0.3,
        help="Smoothing factor for the agent-side RTT EWMA (0 < alpha <= 1)",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> None:
    orchestrator = args.orchestrator.rstrip("/")
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
                logger.error("enrollment failed: %s", exc)
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
                logger.error("initial token refresh failed: %s", exc)
                raise SystemExit(1) from exc
            expires_at = time.time() + expires_in
            logger.info(
                "resuming as %s (node_id=%s) from %s", state.name, state.node_id, state_dir
            )

        rtt_tracker = RttEwma(alpha=args.rtt_ewma_alpha)
        logger.info(
            "starting heartbeat loop: interval=%.1fs orchestrator=%s",
            args.heartbeat_interval_seconds,
            orchestrator,
        )

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
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "heartbeat rejected (%s): %s", exc.response.status_code, exc.response.text
                )
            except httpx.HTTPError as exc:
                logger.error("heartbeat failed (network): %s", exc)

            await asyncio.sleep(args.heartbeat_interval_seconds)


def main() -> None:
    args = parse_args()
    _configure_logging(args.log_level)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("interrupted (Ctrl+C); shutting down cleanly")


if __name__ == "__main__":
    main()
