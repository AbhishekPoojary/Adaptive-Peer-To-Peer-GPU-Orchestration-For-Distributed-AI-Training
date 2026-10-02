"""The agent says so when its connection to the orchestrator is unencrypted."""

from __future__ import annotations

import pytest

from agent.main import plaintext_warning


@pytest.mark.parametrize(
    "url",
    [
        "https://cholesterol-handle.trycloudflare.com",
        "http://localhost:8090",
        "http://127.0.0.1:8090",
        "http://host.docker.internal:8090",
        "http://100.101.102.103:8090",  # Tailscale encrypts this itself
        "http://orchestrator.tail1234.ts.net:8090",
    ],
)
def test_encrypted_or_local_is_quiet(url: str) -> None:
    assert plaintext_warning(url) is None


@pytest.mark.parametrize(
    "url", ["http://192.168.1.10:8090", "http://10.217.9.147:8090", "http://203.0.113.5"]
)
def test_plain_http_to_another_machine_warns(url: str) -> None:
    message = plaintext_warning(url)
    assert message is not None and "unencrypted" in message
