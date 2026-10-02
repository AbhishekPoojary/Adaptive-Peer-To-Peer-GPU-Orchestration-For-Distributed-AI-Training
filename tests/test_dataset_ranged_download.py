"""The trainer downloads a dataset in ranged pieces (trainer/train.py).

A Cloudflare quick tunnel cuts any single request after a minute or two, and a
39 MB archive over one took 116 s -- so a large dataset could never arrive in
one request. Exercised against a real HTTP server on loopback.
"""

from __future__ import annotations

import hashlib
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("torch")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "trainer"))
import train  # noqa: E402

BLOB = bytes(range(256)) * 300  # 76,800 bytes


class _Server(BaseHTTPRequestHandler):
    honour_ranges = True
    fail_once_at: int | None = None
    requests: list[tuple[str | None, str | None]] = []

    def log_message(self, *_args: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        header = self.headers.get("Range")
        type(self).requests.append((header, self.headers.get("Authorization")))
        if not type(self).honour_ranges or header is None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(BLOB)))
            self.end_headers()
            self.wfile.write(BLOB)
            return
        first, _, last = header[6:].partition("-")
        start, end = int(first), min(int(last), len(BLOB) - 1)
        if type(self).fail_once_at == start:
            type(self).fail_once_at = None
            self.send_response(502)
            self.end_headers()
            return
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(BLOB)}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.wfile.write(BLOB[start : end + 1])


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    _Server.honour_ranges = True
    _Server.fail_once_at = None
    _Server.requests = []
    monkeypatch.setattr(train, "_DOWNLOAD_PIECE_BYTES", 10_000)
    monkeypatch.setattr(train.time, "sleep", lambda _s: None)  # no real backoff in tests
    httpd = HTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


def _fetch(url: str, tmp_path: Path) -> bytes:
    destination = tmp_path / "archive.zip"
    train._download_and_verify(
        url, expected_sha256=hashlib.sha256(BLOB).hexdigest(), destination=str(destination)
    )
    return destination.read_bytes()


def test_downloads_in_ranged_pieces(server: str, tmp_path: Path) -> None:
    assert _fetch(f"{server}/leases/l/dataset", tmp_path) == BLOB
    assert len(_Server.requests) == 8  # 76,800 bytes in 10,000-byte pieces


def test_a_failed_piece_is_retried_alone(server: str, tmp_path: Path) -> None:
    _Server.fail_once_at = 30_000
    assert _fetch(f"{server}/leases/l/dataset", tmp_path) == BLOB
    ranges = [r for r, _auth in _Server.requests]
    assert ranges.count("bytes=30000-39999") == 2
    assert ranges.count("bytes=0-9999") == 1, "earlier pieces are not fetched again"


def test_a_server_without_ranges_still_works(server: str, tmp_path: Path) -> None:
    _Server.honour_ranges = False
    assert _fetch(f"{server}/presigned", tmp_path) == BLOB
    assert len(_Server.requests) == 1


def test_the_token_goes_only_to_the_orchestrator(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHECKPOINT_API_URL", f"{server}/api")
    monkeypatch.setenv("CHECKPOINT_TOKEN", "secret")
    _fetch(f"{server}/api/leases/l/dataset", tmp_path)
    assert {auth for _r, auth in _Server.requests} == {"Bearer secret"}
    _Server.requests = []
    _fetch(f"{server}/elsewhere", tmp_path)
    assert {auth for _r, auth in _Server.requests} == {None}


def test_a_corrupted_archive_is_refused(server: str, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="integrity"):
        train._download_and_verify(
            f"{server}/x", expected_sha256="0" * 64, destination=str(tmp_path / "a.zip")
        )
    assert not (tmp_path / "a.zip").exists()
