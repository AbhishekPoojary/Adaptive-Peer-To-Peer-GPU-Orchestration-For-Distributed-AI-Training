"""Agent-side WebSocket client for the log/metric stream (ADR-002, M4).

The agent dials out to the orchestrator (never the reverse — ADR-002) and
forwards real log lines / parsed metric events as JSON frames the moment they
are produced by the trainer container. Nothing is buffered up and flushed at
the end; each frame is sent as soon as it exists.
"""

from __future__ import annotations

import json
import logging
import time
from types import TracebackType
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger("agent.runtime.stream_client")


def stream_url(orchestrator_ws_base: str, *, node_id: str, lease_id: str, epoch: int) -> str:
    """Build the ws(s):// URL for one lease epoch's log/metric stream."""
    base = orchestrator_ws_base.rstrip("/")
    return f"{base}/nodes/{node_id}/leases/{lease_id}/stream?epoch={epoch}"


def http_to_ws_base(orchestrator_http_base: str) -> str:
    """Translate an http(s):// orchestrator base URL to its ws(s):// equivalent."""
    base = orchestrator_http_base.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    return base


class LeaseStreamClient:
    """One WebSocket connection forwarding a single lease's log/metric frames.

    Used as an async context manager so the connection is always closed:

        async with LeaseStreamClient(url=..., access_token=...) as stream:
            await stream.send_log(stream="stdout", line="...")
    """

    def __init__(self, *, url: str, access_token: str) -> None:
        self._url = url
        self._access_token = access_token
        self._connection: ClientConnection | None = None

    async def __aenter__(self) -> LeaseStreamClient:
        self._connection = await websockets.connect(
            self._url,
            additional_headers={"Authorization": f"Bearer {self._access_token}"},
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def send_log(self, *, stream: str, line: str) -> None:
        await self._send({"type": "log", "ts": time.time(), "stream": stream, "line": line})

    async def send_metric(self, *, epoch: int, loss: float, test_accuracy: float | None) -> None:
        await self._send(
            {
                "type": "metric",
                "ts": time.time(),
                "epoch": epoch,
                "loss": loss,
                "test_accuracy": test_accuracy,
            }
        )

    async def _send(self, frame: dict[str, Any]) -> None:
        if self._connection is None:
            raise RuntimeError("LeaseStreamClient used outside its 'async with' context")
        try:
            await self._connection.send(json.dumps(frame))
        except websockets.exceptions.ConnectionClosed as exc:
            # ADR-002: WebSocket frames are not assumed durable. A dropped
            # stream mid-run is not fatal to the lease itself — the REST
            # complete/fail call at the end is what actually matters. Log and
            # move on rather than crashing the training run over a lost
            # transcript frame.
            logger.warning("stream connection closed while sending a frame: %s", exc)
