"""Agent-side lease client and the honest hold/renew loop helper (M2).

The agent claims work the orchestrator scheduled to it, then *holds* the lease —
renewing it before expiry — without executing anything. Execution is M4; until
then the agent is truthful about doing no work ("lease held; execution arrives
in M4") and never fabricates progress or fake training.

A ``--release-after`` test flag makes the agent voluntarily ``/fail`` the lease
with the reason ``execution-not-implemented`` after holding it for a while;
without it the agent holds and renews until interrupted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger("agent.leases")

#: The only reason an M2 agent ever fails a lease — it cannot execute yet.
RELEASE_REASON = "execution-not-implemented"


@dataclass
class HeldLease:
    """A lease this agent currently holds."""

    lease_id: str
    lease_epoch: int
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
        expires_at_ts=_parse_expires(lease),
        held_since_ts=now,
    )


async def claim_lease(
    client: httpx.AsyncClient, *, orchestrator: str, node_id: str, access_token: str
) -> dict[str, Any] | None:
    """Poll for a job scheduled to this node. Returns the lease dict or None."""
    resp = await client.post(
        f"{orchestrator}/nodes/{node_id}/leases/claim",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    lease = resp.json()["lease"]
    return lease if lease is not None else None


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


async def fail_lease(
    client: httpx.AsyncClient,
    *,
    orchestrator: str,
    lease_id: str,
    lease_epoch: int,
    reason: str,
    access_token: str,
) -> dict[str, Any]:
    """Release a held lease via /fail with a truthful reason."""
    resp = await client.post(
        f"{orchestrator}/leases/{lease_id}/fail",
        json={"lease_epoch": lease_epoch, "reason": reason},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data
