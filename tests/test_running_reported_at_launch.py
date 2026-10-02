"""The agent reports a trainer as running when it starts, not on a timer.

The orchestrator records LEASED -> RUNNING on a lease's first renewal. Renewal
used to be purely timer-driven -- due ``margin`` seconds before expiry, so ~10 s
after the claim with a 15 s TTL -- which stamped "training started" that long
after training had in fact started. Every recovery measurement included the
lag. These tests pin the fix: the first renewal goes out as soon as the
execution task signals launch, exactly once, and the timer resumes after it.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from agent.main import ExecutingLease, HeldLease, _service_lease
from agent.runtime.docker_launcher import TrainerLaunchConfig


def _held(*, expires_in: float) -> HeldLease:
    now = time.time()
    return HeldLease(
        lease_id="lease-1",
        lease_epoch=1,
        job_id="job-1",
        expires_at_ts=now + expires_in,
        held_since_ts=now,
    )


async def _cycle(executing: ExecutingLease, renews: list[str]) -> ExecutingLease | None:
    def handler(request: httpx.Request) -> httpx.Response:
        renews.append(request.url.path)
        return httpx.Response(
            200, json={"expires_at": "2099-01-01T00:00:00+00:00"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await _service_lease(
            client,
            orchestrator="http://orchestrator.invalid",
            node_id="node-1",
            access_token="token",
            executing=executing,
            renew_margin_seconds=5.0,
            release_after=None,
            docker_client=None,
            launch_config=TrainerLaunchConfig(image="trainer:test"),
        )


@pytest.mark.asyncio
async def test_no_early_renewal_before_the_trainer_has_launched() -> None:
    task = asyncio.ensure_future(asyncio.sleep(3600))
    try:
        executing = ExecutingLease(held=_held(expires_in=15.0), task=task)
        renews: list[str] = []
        assert await _cycle(executing, renews) is executing
        assert renews == [], "nothing has started, so there is nothing to report"
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_launch_triggers_one_immediate_renewal() -> None:
    task = asyncio.ensure_future(asyncio.sleep(3600))
    try:
        # 15 s left on the lease: the timer alone would not renew for ~10 s.
        executing = ExecutingLease(held=_held(expires_in=15.0), task=task)
        executing.launched.set()
        renews: list[str] = []

        assert await _cycle(executing, renews) is executing
        assert renews == ["/leases/lease-1/renew"]
        assert executing.start_reported is True

        # One-shot: the next cycle is back on the timer. (The mocked renewal
        # pushed expiry far out, so the timer is not due either.)
        await _cycle(executing, renews)
        assert renews == ["/leases/lease-1/renew"]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_failed_launch_report_is_retried_next_cycle() -> None:
    """A network error on the launch-time renewal must not mark it sent --
    otherwise RUNNING would silently fall back to the timer's lag."""
    task = asyncio.ensure_future(asyncio.sleep(3600))
    try:
        executing = ExecutingLease(held=_held(expires_in=15.0), task=task)
        executing.launched.set()

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("orchestrator unreachable", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await _service_lease(
                client,
                orchestrator="http://orchestrator.invalid",
                node_id="node-1",
                access_token="token",
                executing=executing,
                renew_margin_seconds=5.0,
                release_after=None,
                docker_client=None,
                launch_config=TrainerLaunchConfig(image="trainer:test"),
            )
        assert executing.start_reported is False
        assert executing.start_report_due() is True
    finally:
        task.cancel()
