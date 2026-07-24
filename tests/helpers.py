"""Reusable test helpers: real-Postgres provisioning and real Ed25519 crypto.

Nothing here fabricates data that the system under test will treat as real
telemetry: the only "sample" data is an explicitly TEST-labelled hardware
inventory used as a request body, and all keys/signatures are genuine Ed25519.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "orchestrator" / "alembic.ini"

# Admin key the DB-backed API client is configured with.
TEST_ADMIN_KEY = "test-admin-key"

ALL_TABLES = (
    "scheduling_decision_candidates",
    "scheduling_decisions",
    "training_log_lines",
    "training_metrics",
    "leases",
    "job_events",
    "jobs",
    "node_telemetry_samples",
    "auth_challenges",
    "nodes",
    "enrollment_tokens",
)


def base_test_url() -> str:
    """Return TEST_DATABASE_URL or skip the test if it is unset."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL not set; requires a live Postgres "
            "(see deploy/compose.yaml)"
        )
    return url


def asyncpg_dsn(sqlalchemy_url: str) -> str:
    """Convert a SQLAlchemy asyncpg URL to a plain asyncpg DSN."""
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://")


def with_dbname(sqlalchemy_url: str, dbname: str) -> str:
    head, _, _old = sqlalchemy_url.rpartition("/")
    return f"{head}/{dbname}"


async def create_database(admin_dsn: str, dbname: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        await conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await conn.close()


async def drop_database(admin_dsn: str, dbname: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            dbname,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    finally:
        await conn.close()


async def truncate_all(dsn: str) -> None:
    """Empty every M1 table and reset the node-name sequence."""
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
        await conn.execute("ALTER SEQUENCE node_name_seq RESTART")
    finally:
        await conn.close()


async def table_names(dsn: str) -> set[str]:
    """Return the set of public-schema table names in the database."""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    finally:
        await conn.close()
    return {r["tablename"] for r in rows}


def run_alembic(db_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the real Alembic CLI against ``db_url`` in a subprocess.

    Uses a subprocess (not an in-process call) so the migration runs against
    its own event loop exactly as it does on container start.
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = db_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=str(REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


# --- Real Ed25519 crypto -----------------------------------------------------


def generate_node_keypair() -> tuple[Ed25519PrivateKey, str]:
    """Return a fresh Ed25519 private key and its PEM public key (as the agent
    would produce on first run)."""
    private_key = Ed25519PrivateKey.generate()
    return private_key, public_pem(private_key)


def public_pem(private_key: Ed25519PrivateKey) -> str:
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )


def sign_b64(private_key: Ed25519PrivateKey, message: bytes) -> str:
    """Sign ``message`` and return the base64-encoded Ed25519 signature."""
    return base64.b64encode(private_key.sign(message)).decode("ascii")


def sample_hardware(*, with_gpu: bool) -> dict[str, object]:
    """A plausible, clearly-labelled TEST hardware inventory for request bodies."""
    gpus: list[dict[str, object]] = []
    if with_gpu:
        gpus = [
            {
                "name": "TEST-GPU (pytest fixture, not real hardware)",
                "vram_bytes": 8 * 1024**3,
                "driver_version": "test-000.00",
            }
        ]
    return {
        "hostname": "pytest-fixture-host",
        "os": "pytest",
        "cpu_model": "TEST-CPU (pytest fixture, not real hardware)",
        "cores": 4,
        "ram_bytes": 16 * 1024**3,
        "gpus": gpus,
    }


# --- API convenience flows ---------------------------------------------------


async def mint_enrollment_token(
    client: AsyncClient, *, created_by: str = "pytest", ttl_seconds: int | None = None
) -> str:
    """Mint an enrollment token via the admin endpoint and return the raw token."""
    body: dict[str, object] = {"created_by": created_by}
    if ttl_seconds is not None:
        body["ttl_seconds"] = ttl_seconds
    resp = await client.post(
        "/auth/enrollment-tokens", json=body, headers={"X-Admin-Key": TEST_ADMIN_KEY}
    )
    resp.raise_for_status()
    return str(resp.json()["token"])


async def register_new_node(
    client: AsyncClient, *, with_gpu: bool = False
) -> tuple[dict[str, Any], Ed25519PrivateKey]:
    """Mint a token and enroll a fresh node. Returns (register response, key)."""
    token = await mint_enrollment_token(client)
    private_key, pub = generate_node_keypair()
    resp = await client.post(
        "/nodes/register",
        json={
            "enrollment_token": token,
            "public_key": pub,
            "hardware": sample_hardware(with_gpu=with_gpu),
            "agent_version": "test-0.1",
        },
    )
    resp.raise_for_status()
    return resp.json(), private_key


def auth_headers(token: str) -> dict[str, str]:
    """Bearer auth header for a node's access token."""
    return {"Authorization": f"Bearer {token}"}


async def send_heartbeat(
    client: AsyncClient,
    *,
    node_id: str,
    token: str,
    cpu_percent: float = 10.0,
    gpu_util: float | None = None,
) -> dict[str, Any]:
    """Send one heartbeat so the node is ONLINE with a real telemetry sample.

    ``gpu_util`` present -> one GPU telemetry entry; ``None`` -> gpu is null.
    """
    gpu = (
        [
            {
                "util_percent": gpu_util,
                "mem_used_bytes": 0,
                "mem_total_bytes": 8 * 1024**3,
                "temperature_c": None,
                "power_w": None,
            }
        ]
        if gpu_util is not None
        else None
    )
    resp = await client.post(
        f"/nodes/{node_id}/heartbeat",
        json={
            "cpu_percent": cpu_percent,
            "ram_used_bytes": 1,
            "ram_total_bytes": 16 * 1024**3,
            "gpu": gpu,
            "rtt_ms": None,
        },
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


# --- Cohort scheduling fixture (M5) ------------------------------------------

_DEFAULT_JOB_SPEC: dict[str, Any] = {
    "dataset": "mnist",
    "model": "cnn",
    "epochs": 1,
    "batch_size": 32,
    "learning_rate": 0.01,
    "world_size": 1,
    "min_gpu_mem_bytes": None,
}


def schedule_single_rank_job(
    session: AsyncSession,
    *,
    node_id: uuid.UUID,
    spec: dict[str, Any] | None = None,
    scheduler_name: str = "round_robin",
    submitted_by: str = "test",
    epoch: int = 1,
) -> Job:
    """Build a job already SCHEDULED to ``node_id`` as a single-rank cohort.

    Reproduces exactly the state ``services.scheduling.place_job_cohort``
    produces for a world_size=1 placement: the attempt epoch minted, the node
    recorded as rendezvous host, and one rank-0 PENDING lease waiting to be
    claimed. Lets the M2 concurrency/lifecycle tests get a claimable lease
    without running a whole scheduler pass. Adds rows to ``session`` (does not
    commit); the caller commits."""
    job = Job(
        id=uuid.uuid4(),
        spec=spec or dict(_DEFAULT_JOB_SPEC),
        scheduler_name=scheduler_name,
        state=JobState.SCHEDULED,
        scheduled_node_id=node_id,
        rendezvous_node_id=node_id,
        current_lease_epoch=epoch,
        submitted_by=submitted_by,
    )
    session.add(job)
    now = datetime.now(UTC)
    session.add(
        Lease(
            id=uuid.uuid4(),
            job_id=job.id,
            node_id=node_id,
            lease_epoch=epoch,
            rank=0,
            state=LeaseState.PENDING,
            granted_at=now,
            expires_at=now + timedelta(seconds=300),
        )
    )
    return job
