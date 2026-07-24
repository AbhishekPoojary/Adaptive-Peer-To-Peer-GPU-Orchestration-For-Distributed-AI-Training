"""Agent-side lease client and the hold/renew/execute loop helper.

The agent claims work the orchestrator scheduled to it, then holds the lease —
renewing it before expiry — for as long as it takes to run the real trainer
container (M4, ``agent.runtime.execution``). Renewal is independent of
execution progress: a lease is kept alive by timer while its container runs in
the background, however long that takes.

A ``--release-after`` test flag makes the agent voluntarily ``/fail`` the lease
with the reason ``execution-not-implemented`` after holding it for a while
(pre-M4 behaviour, kept for tests that want a lease held-but-not-executed);
without it the agent claims, executes, and reports a real outcome.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger("agent.leases")

#: The only reason an M2-style agent ever failed a lease without executing.
#: Still used by the ``--release-after`` test-only path.
RELEASE_REASON = "execution-not-implemented"


@dataclass
class HeldLease:
    """A lease this agent currently holds."""

    lease_id: str
    lease_epoch: int
    job_id: str
    expires_at_ts: float
    held_since_ts: float

    def seconds_held(self, *, now: float | None = None) -> float:
        return (now or time.time()) - self.held_since_ts

    def renewal_due(self, *, margin_seconds: float, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at_ts - margin_seconds


def _parse_expires(lease: dict[str, Any]) -> float:
    """Parse the lease's ``expires_at`` ISO timestamp to a POSIX float."""
    return datetime.fromisoformat(lease["expires_at"]).timestamp()


def held_from_response(lease: dict[str, Any]) -> HeldLease:
    now = time.time()
    return HeldLease(
        lease_id=lease["id"],
        lease_epoch=int(lease["lease_epoch"]),
        job_id=lease["job_id"],
        expires_at_ts=_parse_expires(lease),
        held_since_ts=now,
    )


async def claim_lease(
    client: httpx.AsyncClient, *, orchestrator: str, node_id: str, access_token: str
) -> dict[str, Any] | None:
    """Poll for a rank slot assigned to this node. Returns the full claim
    response (``{"lease": ..., "rendezvous": ...}``) on a grant, or ``None`` on
    an empty-handed poll. The ``rendezvous`` block (M5) carries the
    rank/world_size/endpoint the caller needs to launch the rank under torchrun.
    """
    resp = await client.post(
        f"{orchestrator}/nodes/{node_id}/leases/claim",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    body: dict[str, Any] = resp.json()
    return body if body.get("lease") is not None else None


async def renew_lease(
    client: httpx.AsyncClient,
    *,
    orchestrator: str,
    lease_id: str,
    lease_epoch: int,
    access_token: str,
) -> dict[str, Any]:
    """Renew a held lease. Raises ``httpx.HTTPStatusError`` on 409 (fenced)."""
    resp = await client.post(
        f"{orchestrator}/leases/{lease_id}/renew",
        json={"lease_epoch": lease_epoch},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data


async def complete_lease(
    client: httpx.AsyncClient,
    *,
    orchestrator: str,
    lease_id: str,
    lease_epoch: int,
    access_token: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Finish a lease successfully, optionally carrying a real training result
    summary (M4: final_loss/final_test_accuracy/epochs_completed/exit_code/
    device — see ``agent.runtime.execution.ExecutionResult``)."""
    body: dict[str, Any] = {"lease_epoch": lease_epoch}
    if result is not None:
        body["result"] = result
    resp = await client.post(
        f"{orchestrator}/leases/{lease_id}/complete",
        json=body,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data


async def fail_lease(
    client: httpx.AsyncClient,
    *,
    orchestrator: str,
    lease_id: str,
    lease_epoch: int,
    reason: str,
    access_token: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Release a held lease via /fail with a truthful reason, optionally
    carrying a real (possibly partial) training result summary (M4)."""
    body: dict[str, Any] = {"lease_epoch": lease_epoch, "reason": reason}
    if result is not None:
        body["result"] = result
    resp = await client.post(
        f"{orchestrator}/leases/{lease_id}/fail",
        json=body,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data


async def fetch_job_spec(
    client: httpx.AsyncClient, *, orchestrator: str, job_id: str
) -> dict[str, Any]:
    """Fetch a job's validated spec (dataset/model/epochs/batch_size/
    learning_rate/...) via the existing read-only ``GET /jobs/{id}`` (dev-mode
    unauthenticated per M2/M8-TODO) — no new endpoint invented for this."""
    resp = await client.get(f"{orchestrator}/jobs/{job_id}")
    resp.raise_for_status()
    spec: dict[str, Any] = resp.json()["spec"]
    return spec
