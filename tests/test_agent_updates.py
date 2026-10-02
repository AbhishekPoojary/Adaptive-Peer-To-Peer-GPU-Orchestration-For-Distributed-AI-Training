"""The agent notices a newer bundle and asks its installer to restart it."""

from __future__ import annotations

import httpx
import pytest

from agent.updates import newer_bundle_version


def _client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_reports_a_different_version() -> None:
    async with _client(lambda r: httpx.Response(200, json={"version": "bbbb"})) as c:
        assert await newer_bundle_version(c, orchestrator="http://o", installed="aaaa") == "bbbb"


@pytest.mark.asyncio
async def test_same_version_is_no_update() -> None:
    async with _client(lambda r: httpx.Response(200, json={"version": "aaaa"})) as c:
        assert await newer_bundle_version(c, orchestrator="http://o", installed="aaaa") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),  # an orchestrator from before this existed
        httpx.Response(500),
        httpx.Response(200, json={}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_a_failed_check_never_triggers_a_restart(response: httpx.Response) -> None:
    async with _client(lambda r: response) as c:
        assert await newer_bundle_version(c, orchestrator="http://o", installed="aaaa") is None


@pytest.mark.asyncio
async def test_unreachable_orchestrator_is_no_update() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async with _client(handler) as c:
        assert await newer_bundle_version(c, orchestrator="http://o", installed="aaaa") is None
