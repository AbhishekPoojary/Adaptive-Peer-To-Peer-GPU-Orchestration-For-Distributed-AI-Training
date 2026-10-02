"""The agent abandons a trainer container when it loses the lease (M7.1).

Epoch fencing already keeps the orchestrator's data safe from a zombie worker:
a late report carrying a stale epoch is rejected. But safety of the *data* is
not the whole story. If the agent keeps the container running after losing the
lease, the node stays occupied by work that now belongs to another node, so it
cannot claim anything new — while the scheduler keeps offering it cohort slots
that then expire unclaimed. That is exactly the failure observed in the wild
(job d2fc2a9f burned 7 reassignment epochs in 108s against a node whose agent
was stuck on an orphaned container).

These tests pin the fix: a rejected renew cancels the execution task, and
cancelling the execution task really does stop the container.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from agent.main import ExecutingLease, HeldLease, _service_lease
from agent.runtime.docker_launcher import TrainerLaunchConfig, stop_container


class FakeStoppableContainer:
    """A container double that records whether it was asked to stop."""

    def __init__(self, *, stop_raises: Exception | None = None) -> None:
        self.id = "fake-container-abandon"
        self.stopped = False
        self.stop_timeout: int | None = None
        self._stop_raises = stop_raises

    def stop(self, **kwargs: Any) -> None:
        if self._stop_raises is not None:
            raise self._stop_raises
        self.stopped = True
        self.stop_timeout = kwargs.get("timeout")

    def logs(self, **kwargs: Any):  # pragma: no cover - not exercised here
        return iter(())

    def wait(self) -> dict[str, Any]:  # pragma: no cover - not exercised here
        return {"StatusCode": 0}


def test_stop_container_issues_the_stop() -> None:
    container = FakeStoppableContainer()
    assert stop_container(container) is True
    assert container.stopped is True
    assert container.stop_timeout == 10


def test_stop_container_never_raises_when_the_container_is_already_gone() -> None:
    """--rm means the container may legitimately have exited and been removed
    before we get here; abandoning is best-effort cleanup on a failure path and
    must not mask the original problem with a new exception."""
    container = FakeStoppableContainer(stop_raises=RuntimeError("404 no such container"))
    assert stop_container(container) is False


@pytest.mark.asyncio
async def test_rejected_renew_cancels_execution_without_stalling_the_agent() -> None:
    """A 409 on renew means the attempt was fenced out. The agent must cancel
    the running execution -- but without waiting for it here.

    Stopping a container takes seconds, and this runs inside the heartbeat
    loop. Awaiting it once silenced a node for 11 s, and the failure detector
    declared a healthy node dead 48 times in one benchmark run. So the cancel
    returns at once, the stop finishes in the background, and no new work is
    claimed until it has (a second trainer would fight the first for the GPU).
    """
    stopped = asyncio.Event()

    async def slow_to_stop() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)  # a container taking its time to go
            stopped.set()
            raise

    task = asyncio.ensure_future(slow_to_stop())
    await asyncio.sleep(0)  # let the task start

    held = HeldLease(
        lease_id="lease-abandoned",
        lease_epoch=1,
        job_id="job-reassigned",
        expires_at_ts=0.0,  # already due, so a renew is attempted this cycle
        held_since_ts=0.0,
    )
    claims: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/renew"):
            return httpx.Response(409, json={"detail": "stale lease epoch"})
        claims.append(request.url.path)
        return httpx.Response(200, json={"lease": None})

    transport = httpx.MockTransport(handler)
    common: dict[str, Any] = {
        "orchestrator": "http://orchestrator.invalid",
        "node_id": "node-1",
        "access_token": "token",
        "renew_margin_seconds": 5.0,
        "release_after": None,
        "docker_client": None,
        "launch_config": TrainerLaunchConfig(image="trainer:test"),
    }
    async with httpx.AsyncClient(transport=transport) as client:
        started = asyncio.get_running_loop().time()
        result = await _service_lease(
            client, executing=ExecutingLease(held=held, task=task), **common  # type: ignore[arg-type]
        )
        assert asyncio.get_running_loop().time() - started < 0.2, "the loop must not wait"
        assert result is None, "the lease is gone; the agent holds nothing"
        assert not stopped.is_set(), "the stop is still in progress in the background"

        # While the old trainer is still stopping, no new work is taken.
        await _service_lease(client, executing=None, **common)
        assert claims == []

        await asyncio.wait_for(stopped.wait(), timeout=5)
        await asyncio.sleep(0)  # let the done-callback run
        assert task.cancelled()

        # Once it is gone, claiming resumes.
        await _service_lease(client, executing=None, **common)
        assert len(claims) == 1


@pytest.mark.asyncio
async def test_cancelling_execution_stops_the_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation of run_lease_execution must reach the container itself --
    the whole point is that the abandoned trainer stops consuming the GPU.

    The log/wait tailer threads run against the fake container, whose logs()
    and wait() are trivially safe, so they need no special handling."""
    from agent.runtime import execution as execution_module

    container = FakeStoppableContainer()
    started = asyncio.Event()

    async def fake_stream_to_completion(**_kwargs: Any) -> None:
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        execution_module, "launch_trainer_container", lambda *a, **k: container
    )
    monkeypatch.setattr(
        execution_module, "_stream_to_completion", fake_stream_to_completion
    )
    monkeypatch.setattr(
        execution_module, "ensure_dataset_cache_volume", lambda *a, **k: None
    )
    monkeypatch.setattr(execution_module, "build_run_kwargs", lambda **k: {})

    task = asyncio.ensure_future(
        execution_module.run_lease_execution(
            # A sentinel, not None: since ADR-007 addendum a None client means
            # "this node runs unsandboxed", and the container path asserts it
            # got a real one. Nothing here touches it — the launcher is patched
            # above — but passing None would now select the wrong backend.
            docker_client=object(),  # type: ignore[arg-type]
            orchestrator_http_base="http://orchestrator.invalid",
            node_id="node-1",
            access_token="token",
            lease_id="lease-abandoned",
            lease_epoch=1,
            job_id="job-reassigned",
            job_spec={},
            has_gpu=False,
            launch_config=TrainerLaunchConfig(image="trainer:test"),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert container.stopped is True, "abandoned container must be stopped"
