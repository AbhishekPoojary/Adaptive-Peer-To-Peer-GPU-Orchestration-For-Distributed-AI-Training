"""The trainer's side of checkpointing through the orchestrator (ADR-006
addendum 3).

``ApiObjectStore`` uses only urllib, so it is exercised here against a real
HTTP server on loopback rather than a mock: what matters is the request that
actually goes over the wire -- method, path, quoting, the bearer header -- and
how real HTTP status codes come back.
"""

from __future__ import annotations

import threading
import urllib.error
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from trainer.checkpoint import (
    ApiObjectStore,
    ObjectNotFoundError,
    S3ObjectStore,
    store_from_env,
)


class _Recorder(BaseHTTPRequestHandler):
    objects: dict[str, bytes] = {}
    seen: list[tuple[str, str, str | None]] = []
    status_override: int | None = None

    def log_message(self, *_args: Any) -> None:  # keep test output clean
        pass

    def _record(self) -> None:
        type(self).seen.append((self.command, self.path, self.headers.get("Authorization")))

    def do_PUT(self) -> None:  # noqa: N802 - http.server's naming
        self._record()
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length)
        if type(self).status_override:
            self.send_response(type(self).status_override or 500)
            self.end_headers()
            return
        type(self).objects[self.path] = data
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        if type(self).status_override:
            self.send_response(type(self).status_override or 500)
            self.end_headers()
            return
        body = type(self).objects.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server() -> Iterator[str]:
    _Recorder.objects = {}
    _Recorder.seen = []
    _Recorder.status_override = None
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


def test_put_then_get_round_trips_with_the_token(server: str) -> None:
    store = ApiObjectStore(api_url=server + "/", lease_id="lease-1", token="tok")
    store.put_bytes("checkpoints/job-1/e001-s00000100-ab.pt", b"weights")
    assert store.get_bytes("checkpoints/job-1/e001-s00000100-ab.pt") == b"weights"

    method, path, auth = _Recorder.seen[0]
    assert method == "PUT"
    assert path == "/leases/lease-1/checkpoint-objects/checkpoints/job-1/e001-s00000100-ab.pt"
    assert auth == "Bearer tok"


def test_404_is_the_protocols_not_found(server: str) -> None:
    """``latest_entry`` relies on this to mean "first attempt"."""
    store = ApiObjectStore(api_url=server, lease_id="lease-1", token="tok")
    with pytest.raises(ObjectNotFoundError):
        store.get_bytes("manifests/job-1.json")


@pytest.mark.parametrize("code", [409, 503])
def test_other_failures_are_not_mistaken_for_absence(server: str, code: int) -> None:
    """A superseded attempt (409) or a storage outage (503) must not read as
    "no checkpoint", which would restart training from zero without a word."""
    _Recorder.status_override = code
    store = ApiObjectStore(api_url=server, lease_id="lease-1", token="tok")
    with pytest.raises(urllib.error.HTTPError) as caught:
        store.get_bytes("manifests/job-1.json")
    assert caught.value.code == code
    with pytest.raises(urllib.error.HTTPError):
        store.put_bytes("manifests/job-1.json", b"{}")


def test_store_selection() -> None:
    assert store_from_env({}) is None
    api = store_from_env(
        {"CHECKPOINT_API_URL": "http://o:8090", "CHECKPOINT_TOKEN": "t", "LEASE_ID": "l"}
    )
    assert isinstance(api, ApiObjectStore)
    # A token without the lease it names is unusable; fall through rather than
    # build a store that would address the wrong path.
    assert store_from_env({"CHECKPOINT_API_URL": "http://o:8090", "CHECKPOINT_TOKEN": "t"}) is None
    assert not isinstance(store_from_env({"S3_ENDPOINT_URL": "http://m"}), S3ObjectStore)
