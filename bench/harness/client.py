"""Authenticated orchestrator client for benchmark scenarios.

Drives the same real HTTP API a human uses, with a real user token (ADR-012) —
not the internal service functions. A benchmark that bypassed the API would
stop measuring the system anyone actually runs, and would not have caught, for
instance, that M8's gating broke the agent's job-spec fetch.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx


class OrchestratorError(Exception):
    """A benchmark's call to the orchestrator failed."""


@dataclass
class BenchClient:
    """Thin authenticated wrapper over the orchestrator's HTTP API."""

    base_url: str
    _client: httpx.AsyncClient
    _token: str

    @classmethod
    async def login(
        cls, *, base_url: str, username: str, password: str, timeout: float = 30.0
    ) -> BenchClient:
        client = httpx.AsyncClient(timeout=timeout)
        resp = await client.post(
            f"{base_url}/auth/login",
            json={"username": username, "password": password},
        )
        if resp.status_code != 200:
            await client.aclose()
            raise OrchestratorError(
                f"benchmark login failed ({resp.status_code}): {resp.text}. "
                f"Create an account with `python -m scripts.create_user`."
            )
        return cls(base_url=base_url, _client=client, _token=resp.json()["access_token"])

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> Any:
        resp = await self._client.get(f"{self.base_url}{path}", headers=self._headers)
        if resp.status_code != 200:
            raise OrchestratorError(f"GET {path} -> {resp.status_code}: {resp.text}")
        return resp.json()

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        resp = await self._client.post(
            f"{self.base_url}{path}", json=body, headers=self._headers
        )
        if resp.status_code >= 400:
            raise OrchestratorError(f"POST {path} -> {resp.status_code}: {resp.text}")
        return resp.json()

    # --- Fleet ---------------------------------------------------------------

    async def nodes(self) -> list[dict[str, Any]]:
        return list((await self._get("/nodes"))["nodes"])

    async def online_nodes(self) -> list[dict[str, Any]]:
        """Nodes the scheduler would actually consider: ONLINE and fresh."""
        return [
            n
            for n in await self.nodes()
            if n["status"] == "ONLINE" and not n["heartbeat_stale"]
        ]

    async def mint_enrollment_token(self, *, created_by: str) -> str:
        body = await self._post(
            "/auth/enrollment-tokens", {"created_by": created_by}
        )
        return str(body["token"])

    # --- Jobs ----------------------------------------------------------------

    async def submit(
        self, *, spec: dict[str, Any], scheduler_name: str
    ) -> dict[str, Any]:
        return dict(
            await self._post(
                "/jobs", {"spec": spec, "scheduler_name": scheduler_name}
            )
        )

    async def job(self, job_id: str) -> dict[str, Any]:
        return dict(await self._get(f"/jobs/{job_id}"))

    async def cancel(self, job_id: str) -> None:
        await self._post(f"/jobs/{job_id}/cancel")

    async def scheduling_decisions(self, job_id: str) -> list[dict[str, Any]]:
        body = await self._get(f"/jobs/{job_id}/scheduling-decisions")
        return list(body["decisions"])

    # --- Waiting -------------------------------------------------------------

    async def wait_for_job_state(
        self,
        job_id: str,
        *,
        states: set[str],
        timeout_seconds: float,
        poll_seconds: float = 1.0,
    ) -> tuple[dict[str, Any], float]:
        """Poll until the job reaches one of ``states``.

        Returns ``(job, elapsed_seconds)``. Raises on timeout rather than
        returning the last-seen state: a scenario that silently accepted "still
        RUNNING" as an outcome would publish a measurement of nothing.

        Elapsed time is from ``time.monotonic`` so a clock adjustment mid-run
        cannot produce a negative or inflated duration.
        """
        started = time.monotonic()
        while True:
            job = await self.job(job_id)
            if job["state"] in states:
                return job, time.monotonic() - started
            if time.monotonic() - started > timeout_seconds:
                raise TimeoutError(
                    f"job {job_id[:8]} was {job['state']} after "
                    f"{timeout_seconds:.0f}s, waiting for one of {sorted(states)}"
                )
            await asyncio.sleep(poll_seconds)

    async def wait_for_online_nodes(
        self, *, count: int, timeout_seconds: float, poll_seconds: float = 1.0
    ) -> list[dict[str, Any]]:
        """Wait until at least ``count`` nodes are ONLINE with fresh heartbeats."""
        started = time.monotonic()
        while True:
            online = await self.online_nodes()
            if len(online) >= count:
                return online
            if time.monotonic() - started > timeout_seconds:
                raise TimeoutError(
                    f"only {len(online)}/{count} nodes came online within "
                    f"{timeout_seconds:.0f}s — is an agent running?"
                )
            await asyncio.sleep(poll_seconds)
